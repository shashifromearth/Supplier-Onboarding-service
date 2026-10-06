"""LLM second reader: text-only, grounded, TIN-redacted. Provider-agnostic (see llm_clients.py).

The model reads *text* fields from the parsed document (LlamaParse Markdown or OCR lines). It cannot see
pixels, so for checkbox and signature it abstains (`NOT_ASSESSED`) and the layout reader decides. Every value
must cite the numbered input lines it came from; values not found in those lines are penalised. The TIN is
redacted from the input and never requested. Decisions are made by code, never by the model.

  LLMExtractor  one provider: prompt -> JSON -> validated, grounded FieldCandidates (one retry on bad JSON)
  LLMChain      providers in priority order; the first that returns a usable result wins (Gemini -> Groq)
"""
from __future__ import annotations

import json
import re
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from w9_onboarding.infrastructure.llm_clients import LLMClient, clients_from_env
from w9_onboarding.config import PROMPTS_DIR
from w9_onboarding.domain.models import ExtractedW9, FieldCandidate, OcrLine, OcrResult
from w9_onboarding.utils.normalize import parse_date, squash, tin_from_text
from w9_onboarding.utils.observability import get_logger, log_event
from w9_onboarding.utils.security import TIN_LIKE

PROMPTS = PROMPTS_DIR
_log = get_logger("w9.llm")

LLM_FIELDS = ["legal_name", "dba_name", "tax_classification", "address", "city", "state", "zip",
              "tin_box", "signature_present", "signature_date"]
VISUAL_FIELDS = ("signature_present", "tax_classification")
_SELF_CONF = {"high": 1.0, "medium": 0.85, "low": 0.6}


class LLMExtractionError(Exception):
    """No usable answer: provider failure, or invalid JSON after one retry."""


class LLMItem(BaseModel):
    value: Optional[Union[bool, str]] = None
    source_line_ids: list[str] = Field(default_factory=list)
    certainty: Literal["high", "medium", "low"] = "low"


class LLMPayload(BaseModel):
    form_type: str = "W-9"
    fields: dict[str, LLMItem] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


def parse_llm_json(text: str) -> LLMPayload:
    """Tolerant parse: strips code fences / surrounding prose, then validates the shape with Pydantic."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in model output")
    return LLMPayload.model_validate(json.loads(t[start:end + 1]))


def _normalise_form_type(v: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", (v or "").upper())
    if s == "W9":
        return "W-9"
    if s.startswith("W8"):
        return "W-8" + s[2:]
    return "UNKNOWN" if s in ("", "OTHER", "UNKNOWN") else s


def llm_input_lines(ocr: OcrResult) -> list[tuple[str, OcrLine]]:
    """Numbered, TIN-redacted lines the LLM will read and cite. Prefers the parser's Markdown."""
    base_conf = ocr.page_confidence if ocr.page_confidence is not None else (ocr.mean_confidence() or 0.9)
    if ocr.markdown:
        rows = [t for t in (ln.strip() for ln in ocr.markdown.splitlines()) if t and not re.fullmatch(r"[\s|:\-]+", t)]
        raw = [OcrLine(text=t, bbox=(0, 0, 0, 0), confidence=base_conf) for t in rows]
    else:
        raw = [l for l in ocr.lines if l.page == 1]
    out: list[tuple[str, OcrLine]] = []
    for i, l in enumerate(raw):
        digits = re.sub(r"\D", "", l.text)
        text = "[TIN REDACTED]" if (len(digits) == 9 and len(l.text) <= 14) else TIN_LIKE.sub("[TIN REDACTED]", l.text)
        out.append((f"L{i}", l.model_copy(update={"text": text})))
    return out


class LLMExtractor:
    has_vision = False

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.name = f"llm-grounded-v1[{client.provider}]"

    def build_prompt(self, ocr: OcrResult) -> tuple[str, str, dict[str, OcrLine]]:
        numbered = llm_input_lines(ocr)
        if ocr.markdown:
            source, body = "document text parsed from page 1 (Markdown)", "\n".join(f"[{i}] {l.text}" for i, l in numbered)
        else:
            source = "OCR lines from page 1 with pixel positions"
            body = "\n".join(f"[{i}] (x={int(l.bbox[0])},y={int(l.bbox[1])}) {l.text}" for i, l in numbered)
        system = (PROMPTS / "w9_extraction_system.md").read_text(encoding="utf-8")
        user = (PROMPTS / "w9_extraction_user.md").read_text(encoding="utf-8").replace("{{SOURCE}}", source).replace("{{OCR_LINES}}", body)
        return system, user, dict(numbered)

    def extract(self, ocr: OcrResult) -> ExtractedW9:
        system, user, by_id = self.build_prompt(ocr)
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        payload, err = None, ""
        for attempt in range(2):
            prompt = user if attempt == 0 else f"{user}\n\nYour previous reply was invalid ({err}). Reply with ONLY the JSON object."
            text, toks = self.client.complete(system, prompt)          # LLMCallError propagates (provider failure)
            usage = {k: usage[k] + toks.get(k, 0) for k in usage}
            try:
                payload = parse_llm_json(text)
                break
            except ValueError as e:
                err = str(e)[:160].replace("\n", " ")
        if payload is None:
            raise LLMExtractionError(f"{self.client.provider}: invalid JSON twice: {err}")
        out = self.from_payload(payload, by_id)
        out.llm_usage, out.llm_provider = usage, self.client.provider
        hit = tin_from_text(ocr.full_text)        # deterministic TIN; the Hybrid prefers the positional read
        if hit:
            out.tin = FieldCandidate(value=hit[0], confidence=hit[2], source="text:tin-pattern", notes=hit[3])
            if out.tin_type.value is None:
                out.tin_type = FieldCandidate(value=hit[1], confidence=hit[2], source="text:tin-pattern")
        return out

    def from_payload(self, payload: "LLMPayload | dict", by_id: dict[str, OcrLine]) -> ExtractedW9:
        """Turn the model's JSON into FieldCandidates, scoring confidence by grounding."""
        p = payload if isinstance(payload, LLMPayload) else LLMPayload.model_validate(payload)
        out = ExtractedW9(extractor=self.name)
        out.form_type = FieldCandidate(value=_normalise_form_type(p.form_type), confidence=0.9, source=self.name,
                                       notes=[f"model issue: {i}" for i in p.issues[:3]])
        for f in LLM_FIELDS:
            item = p.fields.get(f) or LLMItem()
            val, cert = item.value, item.certainty
            if isinstance(val, str) and f == "signature_present":
                val = {"true": True, "false": False}.get(val.strip().lower())
            if isinstance(val, str):
                val = val.strip() or None
            cited = [by_id[i] for i in item.source_line_ids if i in by_id]
            ocr_conf = sum(l.confidence for l in cited) / len(cited) if cited else 0.0
            notes: list[str] = []
            if f in VISUAL_FIELDS:        # text-only model: abstain unless the text shows an explicit marker
                if val is None:
                    fc = FieldCandidate(value=None, confidence=0.0, source=self.name,
                                        notes=["NOT_ASSESSED: text-only reader cannot see marks"])
                else:
                    fc = FieldCandidate(value=val, confidence=round(0.9 * _SELF_CONF[cert] * 0.8, 3), source=self.name,
                                        notes=["inferred from a text marker in the parsed document (no image)"])
            elif f == "tin_box":
                conf = (0.9 if cited else 0.6) * _SELF_CONF[cert]
                fc = FieldCandidate(value=val, confidence=round(conf, 3), source=self.name)
            elif val is None:
                fc = FieldCandidate(value=None, confidence=round(0.85 * _SELF_CONF[cert], 3), source=self.name)
            else:
                grounded = any(fuzz.partial_ratio(squash(str(val)), squash(l.text)) >= 85 for l in cited)
                if not grounded:
                    notes.append("UNGROUNDED: value not found in cited input lines")
                conf = (ocr_conf if cited else 0.5) * _SELF_CONF[cert] * (1.0 if grounded else 0.5)
                fc = FieldCandidate(value=val, confidence=round(min(conf, 0.99), 3), source=self.name, notes=notes)
            if f == "tin_box":
                out.tin_type = fc.model_copy(update={"value": {"SSN": "SSN", "EIN": "EIN"}.get(str(val).upper()) if val else None})
            elif f == "signature_date" and fc.value:
                d = parse_date(str(fc.value))
                fc.value = d.isoformat() if d else None
                setattr(out, f, fc)
            else:
                setattr(out, f, fc)
        return out


class LLMChain:
    """Providers in priority order. A provider that fails (HTTP error, timeout, or unusable output) is skipped
    and the next one is tried; if all fail the caller (HybridExtractor) degrades to the layout reader."""
    has_vision = False

    def __init__(self, extractors: list[LLMExtractor]) -> None:
        self.extractors = extractors
        self.name = "llm-chain[" + ">".join(e.client.provider for e in extractors) + "]"

    @classmethod
    def from_env(cls) -> "LLMChain":
        return cls([LLMExtractor(c) for c in clients_from_env()])

    @staticmethod
    def available() -> bool:
        return bool(clients_from_env())

    def extract(self, ocr: OcrResult) -> ExtractedW9:
        errors: list[str] = []
        for ex in self.extractors:
            try:
                return ex.extract(ocr)
            except Exception as e:
                errors.append(f"{ex.client.provider}: {type(e).__name__}")
                log_event(_log, "llm_provider_failed", provider=ex.client.provider, reason=type(e).__name__,
                          status=getattr(e, "status_code", None), detail=str(e)[:120],
                          next=("fallback" if ex is not self.extractors[-1] else "none"))
        raise LLMExtractionError("all LLM providers failed: " + "; ".join(errors) if errors else "no LLM provider configured")
