"""Public response contract v1 (what the service returns). Built from domain models by services.response_builder.

The JSON schema in schemas/ is generated from OnboardingResponse."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from w9_onboarding.config import SCHEMA_VERSION
from w9_onboarding.domain.models import Decision, MatchResult, ReviewReason, SuggestedAction, TinType, ValidationFlag

# ---------------------------------------------------------------------------
# Public API contract (v1)
# ---------------------------------------------------------------------------
class ExtractedField(BaseModel):
    value: Any = Field(None, description="Normalised value, or null if not found / not readable.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Calibrated confidence the value is correct (0-1).")
    source: str = Field("", description="Which extractor/stage produced the value.")
    status: str = Field("OK", description="OK | LOW_CONFIDENCE | MISSING | NOT_PROVIDED (optional field confidently blank)")


class TinField(BaseModel):
    """The TIN is never returned in clear. Consumers use vault_ref to retrieve it with their own entitlement."""
    masked: Optional[str] = Field(None, description="e.g. '**-***6789' or '***-**-6789'")
    last4: Optional[str] = None
    tin_type: TinType = TinType.UNKNOWN
    fingerprint: Optional[str] = Field(None, description="Tenant-scoped HMAC-SHA256 blind index; stable for equality checks.")
    vault_ref: Optional[str] = Field(None, description="Opaque reference to the encrypted TIN in the tenant vault.")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    source: str = ""
    status: str = "OK"


class ExtractedFields(BaseModel):
    legal_name: ExtractedField
    dba_name: ExtractedField
    tax_classification: ExtractedField
    address: ExtractedField
    city: ExtractedField
    state: ExtractedField
    zip: ExtractedField
    tin: TinField
    signature_present: ExtractedField
    signature_date: ExtractedField


class DocumentInfo(BaseModel):
    filename: str
    sha256: str
    mime_type: str
    page_count: int
    form_type: Optional[str] = None
    form_revision: Optional[str] = None


class ProcessingInfo(BaseModel):
    pipeline_version: str
    policy_version: str
    ocr_provider: str
    extractor: str
    latency_ms: dict[str, float]
    estimated_cost_usd: float
    processed_at: datetime


class OnboardingResponse(BaseModel):
    """v1 contract returned to downstream consumers (supplier-master service, review queue)."""
    schema_version: str = SCHEMA_VERSION
    request_id: str
    tenant_id: str
    document_id: str
    decision: Decision
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence that `decision` is the correct action.")
    matched_supplier_id: Optional[str] = Field(None, description="Set only for UPDATE_EXISTING_SUPPLIER.")
    suggested_action: Optional[SuggestedAction] = Field(None, description="Set only for HUMAN_REVIEW.")
    review_reasons: list[ReviewReason] = Field(default_factory=list)
    document: DocumentInfo
    extracted_fields: ExtractedFields
    validation_flags: list[ValidationFlag] = Field(default_factory=list)
    match_result: MatchResult
    reasoning: list[str] = Field(default_factory=list, description="Human-readable decision trace, in order.")
    processing: ProcessingInfo
