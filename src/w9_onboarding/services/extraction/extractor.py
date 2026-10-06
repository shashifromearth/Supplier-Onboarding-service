"""Extraction layer: OcrResult -> ExtractedW9 with per-field confidence.

Readers share one interface (W9Extractor), one module each:

  layout_extractor.LayoutExtractor  deterministic anchors + pixel analysis (checkbox, signature ink)
  llm_extractor.LLMChain            grounded text-only LLM (Gemini first, Groq fallback), TIN-redacted
  HybridExtractor (here)            runs both and reconciles per field; the production default

Agreement raises confidence, disagreement lowers it and is surfaced. If the LLM is unavailable the result
is the layout reader's, flagged SECOND_READER_UNAVAILABLE. Decisions are never made here.
"""
from __future__ import annotations

from typing import Protocol

from w9_onboarding.services.extraction.layout_extractor import LayoutExtractor
from w9_onboarding.services.extraction.llm_extractor import LLMChain
from w9_onboarding.domain.models import ExtractedW9, FieldCandidate, OcrResult
from w9_onboarding.utils.observability import get_logger, log_event
from rapidfuzz import fuzz
from w9_onboarding.utils.normalize import squash

_log = get_logger("w9.extractor")


class W9Extractor(Protocol):
    name: str

    def extract(self, ocr: OcrResult) -> ExtractedW9: ...


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    return fuzz.ratio(squash(str(a)), squash(str(b))) >= 92


def _has_opinion(fc: FieldCandidate) -> bool:
    """A reader that *could not read* a field (null, low confidence) has no opinion; null with high
    confidence ("this optional field is blank") is an opinion."""
    return not (fc.value is None and fc.confidence < 0.5)


class HybridExtractor:
    name = "hybrid(layout+llm)-v1"

    def __init__(self, layout: LayoutExtractor | None = None, llm: "W9Extractor | None" = None) -> None:
        self.layout = layout or LayoutExtractor()
        self.llm = llm or LLMChain.from_env()

    def extract(self, ocr: OcrResult) -> ExtractedW9:
        base = self.layout.extract(ocr)
        if base.form_type.value.startswith("W-8") and base.form_type.confidence > 0.9:
            return base
        try:
            other = self.llm.extract(ocr)
        except Exception as e:  # LLM down / bad JSON: degrade to the deterministic reader, flag it, never crash
            log_event(_log, "second_reader_unavailable", reason=type(e).__name__, detail=str(e)[:120])
            base.warnings.append("SECOND_READER_UNAVAILABLE")
            base.extractor = f"{self.name}[layout-only]"
            return base
        if base.tin.value is None and other.tin.value is not None:   # keep positional TIN when present
            base.tin, base.tin_type = other.tin, other.tin_type
        return reconcile(base, other, name=self.name)


def reconcile(a: ExtractedW9, b: ExtractedW9, name: str) -> ExtractedW9:
    """Per-field merge of reader A (layout) and reader B (LLM).

    agree -> noisy-OR boost | disagree -> keep the stronger, penalise, flag | one reader has no opinion -> use the other.
    """
    out = a.model_copy(deep=True)
    out.extractor = f"{name}[{b.llm_provider}]" if b.llm_provider else name   # records which LLM answered
    out.llm_usage, out.llm_provider = b.llm_usage, b.llm_provider
    if a.form_type.value == "UNKNOWN" and b.form_type.value == "W-9":
        out.form_type = b.form_type.model_copy(update={"confidence": 0.8, "notes": ["layout header unreadable; LLM read W-9"]})
    for f in ExtractedW9.FIELD_NAMES:
        if f == "tin":
            continue  # TIN is deterministic-only by design (never sent to the LLM)
        fa, fb = getattr(a, f), getattr(b, f)
        if any(n.startswith("NOT_ASSESSED") for n in fb.notes) or (_has_opinion(fa) and not _has_opinion(fb)):
            setattr(out, f, fa.model_copy(update={"notes": fa.notes + ["single reader (second reader abstained)"]}))
        elif not _has_opinion(fa) and _has_opinion(fb):
            setattr(out, f, fb.model_copy(update={"confidence": round(fb.confidence * 0.9, 3),
                                                  "notes": fb.notes + ["single reader (layout reader could not read)"]}))
        elif _same(fa.value, fb.value):
            conf = 1 - (1 - fa.confidence) * (1 - fb.confidence)
            winner = fa if fa.value is not None else fb
            setattr(out, f, winner.model_copy(update={"confidence": round(min(conf, 0.99), 3),
                                                      "source": f"{fa.source}+{fb.source}", "notes": fa.notes + ["extractors agree"]}))
        else:
            winner, loser = (fa, fb) if fa.confidence >= fb.confidence else (fb, fa)
            setattr(out, f, winner.model_copy(update={
                "confidence": round(min(fa.confidence, fb.confidence) * 0.6, 3),
                "notes": winner.notes + [f"EXTRACTOR_DISAGREEMENT: other extractor read a different value ({loser.source})"]}))
    return out


def get_extractor(name: str) -> W9Extractor:
    if name == "layout":
        return LayoutExtractor()
    if name == "llm":
        return LLMChain.from_env()
    if name == "hybrid":
        return HybridExtractor()
    if name == "auto":
        return HybridExtractor() if LLMChain.available() else LayoutExtractor()
    raise ValueError(f"unknown extractor '{name}'")
