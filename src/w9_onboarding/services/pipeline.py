"""Orchestration: Upload -> OCR -> Extraction -> Validation -> Matching -> Decision -> JSON.

Each stage is a separate module with a typed input/output, so stages can be swapped
(OCR provider, extractor) or scaled independently (queue workers in production).
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from w9_onboarding import config
from w9_onboarding.services.decision_engine import decide
from w9_onboarding.services.extraction.extractor import W9Extractor, get_extractor
from w9_onboarding.services.matcher import Matcher
from w9_onboarding.domain.models import ExtractedW9, FieldCandidate, MatchResult, OcrResult
from w9_onboarding.dto.onboarding_response import DocumentInfo, OnboardingResponse
from w9_onboarding.utils.observability import METRICS, get_logger, log_event, span, trace_context
from w9_onboarding.infrastructure.ocr import OcrError, load_document, run_ocr
from w9_onboarding.services.response_builder import build_response
from w9_onboarding.repositories.tin_vault import TinVault
from w9_onboarding.repositories.supplier_master import CsvSupplierMaster, SupplierMasterRepository
from w9_onboarding.services.validator import validate

log = get_logger("w9.pipeline")


def estimate_cost(ocr: OcrResult | None, x: ExtractedW9 | None) -> float:
    cost = ocr.cost_usd if ocr else 0.0                      # LlamaParse credits; 0 for local readers
    if x and x.llm_usage:
        p_in, p_out = config.LLM_PRICES_PER_1M.get(x.llm_provider or "", (0.0, 0.0))
        cost += x.llm_usage.get("prompt_tokens", 0) / 1e6 * p_in
        cost += x.llm_usage.get("completion_tokens", 0) / 1e6 * p_out
    return cost


class OnboardingPipeline:
    def __init__(self, tenant_id: str, extractor: "str | W9Extractor" = "auto", ocr_provider: str = "auto",
                 repo: Optional[SupplierMasterRepository] = None, today: Optional[date] = None) -> None:
        self.tenant_id = tenant_id
        self.repo = repo or CsvSupplierMaster(tenant_id)   # tenant-scoped at construction
        self.extractor: W9Extractor = get_extractor(extractor) if isinstance(extractor, str) else extractor
        self.ocr_provider = ocr_provider
        self.today = today
        self.vault = TinVault()

    def process(self, path: str | Path, document_id: Optional[str] = None) -> OnboardingResponse:
        request_id = uuid.uuid4().hex
        timings: dict[str, float] = {}
        with trace_context(tenant_id=self.tenant_id, request_id=request_id):
            doc = None
            ocr: OcrResult | None = None
            try:
                with span("load", timings, log):
                    doc = load_document(path)
                document_id = document_id or f"doc_{doc.sha256[:16]}"  # content-addressed => idempotent retries
                with span("ocr", timings, log):
                    ocr = run_ocr(doc, self.ocr_provider)
                with span("extract", timings, log):
                    x = self.extractor.extract(ocr)
            except OcrError as e:
                # Unreadable document: still return a well-formed response that says "I don't know".
                METRICS.inc("w9_unreadable_documents_total", tenant=self.tenant_id)
                log_event(log, "document_unreadable", error=str(e))
                x = ExtractedW9(extractor="none", form_type=FieldCandidate(value="UNREADABLE", confidence=0.0, notes=[str(e)]))
                document_id = document_id or f"doc_{uuid.uuid4().hex[:16]}"

            with span("validate", timings, log):
                x, flags = validate(x, today=self.today)
            with span("match", timings, log):
                match = Matcher(self.repo).match(x) if x.form_type.value == "W-9" else MatchResult(strategy="NONE")
            with span("decide", timings, log):
                outcome = decide(x, flags, match)

            resp = build_response(
                request_id=request_id, tenant_id=self.tenant_id, document_id=document_id,
                doc_info=DocumentInfo(filename=Path(path).name, sha256=doc.sha256 if doc else "",
                                      mime_type=doc.mime_type if doc else "unknown",
                                      page_count=len(doc.page_images) if doc else 0,
                                      form_type=x.form_type.value, form_revision=x.form_revision.value),
                x=x, flags=flags, match=match, outcome=outcome,
                ocr_provider=ocr.provider if ocr else "none", latency_ms=timings,
                cost_usd=estimate_cost(ocr, x), vault=self.vault)

            # --- metrics: the operational surface (see DESIGN.md Part 4) ---
            METRICS.inc("w9_documents_total", tenant=self.tenant_id, decision=resp.decision.value)
            METRICS.observe("w9_decision_confidence", resp.confidence, decision=resp.decision.value)
            for name in ("legal_name", "tax_classification", "address", "signature_present"):
                METRICS.observe("w9_field_confidence", getattr(resp.extracted_fields, name).confidence, field=name)
            METRICS.observe("w9_field_confidence", resp.extracted_fields.tin.confidence, field="tin")
            if match.best_candidate:
                METRICS.observe("w9_best_match_composite", match.best_candidate.scores.composite)
            for f in flags:
                METRICS.inc("w9_validation_flags_total", code=f.code, severity=f.severity.value)
            for r in resp.review_reasons:
                METRICS.inc("w9_review_reasons_total", code=r.code)
            log_event(log, "document_processed", document_id=document_id, decision=resp.decision.value,
                      confidence=resp.confidence, review_codes=[r.code for r in resp.review_reasons],
                      match_strategy=match.strategy, extractor=x.extractor, ocr=resp.processing.ocr_provider,
                      total_ms=round(sum(timings.values()), 1))
            return resp
