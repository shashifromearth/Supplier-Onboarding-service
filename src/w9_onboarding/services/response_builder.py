"""Build the public, PII-safe OnboardingResponse (contract v1) from internal objects.

This is the only place internal models are converted to the external contract, so it is
the single choke point for redaction: the plaintext TIN goes into the vault here and never
appears in the response.
"""
from __future__ import annotations

from datetime import datetime, timezone

from w9_onboarding.config import POLICY, POLICY_VERSION, Policy
from w9_onboarding.services.decision_engine import DecisionOutcome
from w9_onboarding.domain.models import ExtractedW9, FieldCandidate, MatchResult, TinType, ValidationFlag
from w9_onboarding.dto.onboarding_response import (DocumentInfo, ExtractedField, ExtractedFields, OnboardingResponse,
                                                   ProcessingInfo, TinField)
from w9_onboarding.utils.security import mask_tin, tin_fingerprint
from w9_onboarding.repositories.tin_vault import TinVault

PIPELINE_VERSION = "0.3.0"


def _status(fc: FieldCandidate, field: str, policy: Policy) -> str:
    if fc.value is None:
        return "NOT_PROVIDED" if field == "dba_name" and fc.confidence >= 0.7 else "MISSING"
    critical = field in policy.confidence.critical_fields
    th = policy.confidence.critical_field_min if critical else policy.confidence.standard_field_min
    return "OK" if fc.confidence >= th else "LOW_CONFIDENCE"


def _field(x: ExtractedW9, name: str, policy: Policy) -> ExtractedField:
    fc: FieldCandidate = getattr(x, name)
    return ExtractedField(value=fc.value, confidence=round(fc.confidence, 3), source=fc.source, status=_status(fc, name, policy))


def build_response(*, request_id: str, tenant_id: str, document_id: str, doc_info: DocumentInfo, x: ExtractedW9,
                   flags: list[ValidationFlag], match: MatchResult, outcome: DecisionOutcome, ocr_provider: str,
                   latency_ms: dict[str, float], cost_usd: float, vault: TinVault, policy: Policy = POLICY) -> OnboardingResponse:
    tin_digits = x.tin.value
    tin_type = x.tin_type.value or "UNKNOWN"
    tin = TinField(
        masked=mask_tin(tin_digits, tin_type), last4=tin_digits[-4:] if tin_digits else None,
        tin_type=TinType(tin_type) if tin_type in ("EIN", "SSN") else TinType.UNKNOWN,
        fingerprint=tin_fingerprint(tenant_id, tin_digits) if tin_digits else None,
        vault_ref=vault.put(tenant_id, tin_digits) if tin_digits else None,
        confidence=round(x.tin.confidence, 3), source=x.tin.source, status=_status(x.tin, "tin", policy))
    fields = ExtractedFields(
        legal_name=_field(x, "legal_name", policy), dba_name=_field(x, "dba_name", policy),
        tax_classification=_field(x, "tax_classification", policy), address=_field(x, "address", policy),
        city=_field(x, "city", policy), state=_field(x, "state", policy), zip=_field(x, "zip", policy), tin=tin,
        signature_present=_field(x, "signature_present", policy), signature_date=_field(x, "signature_date", policy))
    return OnboardingResponse(
        request_id=request_id, tenant_id=tenant_id, document_id=document_id,
        decision=outcome.decision, confidence=outcome.confidence, matched_supplier_id=outcome.matched_supplier_id,
        suggested_action=outcome.suggested_action, review_reasons=outcome.review_reasons, document=doc_info,
        extracted_fields=fields, validation_flags=flags, match_result=match, reasoning=outcome.reasoning,
        processing=ProcessingInfo(pipeline_version=PIPELINE_VERSION, policy_version=POLICY_VERSION, ocr_provider=ocr_provider,
                                  extractor=x.extractor, latency_ms=latency_ms, estimated_cost_usd=round(cost_usd, 5),
                                  processed_at=datetime.now(timezone.utc)))
