"""Domain models: OCR evidence, extracted fields, supplier records, validation / match / decision value objects.

Plain Pydantic models and enums with no dependency on any other layer. The public response contract lives in dto/."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums (string enums so they serialise as stable strings in the contract)
# ---------------------------------------------------------------------------
class Decision(str, Enum):
    CREATE_NEW_SUPPLIER = "CREATE_NEW_SUPPLIER"
    UPDATE_EXISTING_SUPPLIER = "UPDATE_EXISTING_SUPPLIER"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class TaxClassification(str, Enum):
    INDIVIDUAL_SOLE_PROPRIETOR = "INDIVIDUAL_SOLE_PROPRIETOR"
    C_CORPORATION = "C_CORPORATION"
    S_CORPORATION = "S_CORPORATION"
    PARTNERSHIP = "PARTNERSHIP"
    TRUST_ESTATE = "TRUST_ESTATE"
    LLC_C = "LLC_C"
    LLC_S = "LLC_S"
    LLC_P = "LLC_P"
    LLC_UNSPECIFIED = "LLC_UNSPECIFIED"
    OTHER = "OTHER"


class TinType(str, Enum):
    EIN = "EIN"
    SSN = "SSN"
    UNKNOWN = "UNKNOWN"


class Severity(str, Enum):
    BLOCKING = "BLOCKING"   # automation must not proceed -> HUMAN_REVIEW
    WARNING = "WARNING"     # surfaced to consumer, does not block on its own
    INFO = "INFO"


class MatchBand(str, Enum):
    AUTO_MATCH = "AUTO_MATCH"
    REVIEW = "REVIEW"
    NO_MATCH = "NO_MATCH"


# ---------------------------------------------------------------------------
# Internal: OCR
# ---------------------------------------------------------------------------
class OcrLine(BaseModel):
    """A line of text with pixel bbox (x0, y0, x1, y1) on the rendered page."""
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0.0, le=1.0)
    page: int = 1


class OcrResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: str
    lines: list[OcrLine]
    page_count: int
    dpi: int
    # Rendered page images (PIL) for checkbox / signature ink analysis. Not serialised.
    page_images: list[Any] = Field(default_factory=list, exclude=True)
    image_bytes_png: Optional[bytes] = Field(default=None, exclude=True)  # page 1 (kept for vision-capable readers)
    # Markdown rendering from a managed parser (LlamaParse). None for text-layer / local OCR.
    markdown: Optional[str] = Field(default=None, exclude=True)
    page_confidence: Optional[float] = None   # provider's own page/document confidence, if it reports one
    cost_usd: float = 0.0                     # provider cost for this document (0 for local providers)

    @property
    def full_text(self) -> str:
        """Parser Markdown when available, otherwise the OCR lines joined."""
        return self.markdown or "\n".join(l.text for l in self.lines)

    def mean_confidence(self) -> float:
        return sum(l.confidence for l in self.lines) / len(self.lines) if self.lines else 0.0


# ---------------------------------------------------------------------------
# Internal: extraction
# ---------------------------------------------------------------------------
class FieldCandidate(BaseModel):
    """One extracted field before it is made PII-safe for the response."""
    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: str = ""                 # e.g. "layout:line1", "gpt-4o", "ink-density"
    evidence: Optional[str] = None   # OCR text the value was read from (internal only)
    notes: list[str] = Field(default_factory=list)


class ExtractedW9(BaseModel):
    """Raw extraction. Holds the plaintext TIN -> internal only."""
    form_type: FieldCandidate = Field(default_factory=FieldCandidate)       # "W-9" | "W-8BEN" | ...
    form_revision: FieldCandidate = Field(default_factory=FieldCandidate)
    legal_name: FieldCandidate = Field(default_factory=FieldCandidate)
    dba_name: FieldCandidate = Field(default_factory=FieldCandidate)
    tax_classification: FieldCandidate = Field(default_factory=FieldCandidate)
    address: FieldCandidate = Field(default_factory=FieldCandidate)
    city: FieldCandidate = Field(default_factory=FieldCandidate)
    state: FieldCandidate = Field(default_factory=FieldCandidate)
    zip: FieldCandidate = Field(default_factory=FieldCandidate)
    tin: FieldCandidate = Field(default_factory=FieldCandidate)             # digits only
    tin_type: FieldCandidate = Field(default_factory=FieldCandidate)
    signature_present: FieldCandidate = Field(default_factory=FieldCandidate)
    signature_date: FieldCandidate = Field(default_factory=FieldCandidate)  # ISO date str

    extractor: str = ""
    llm_usage: dict[str, int] = Field(default_factory=dict)  # prompt/completion tokens
    warnings: list[str] = Field(default_factory=list)        # e.g. SECOND_READER_UNAVAILABLE
    llm_provider: Optional[str] = None                       # which LLM actually answered ("gemini" / "groq")

    FIELD_NAMES: ClassVar[tuple[str, ...]] = (
        "legal_name", "dba_name", "tax_classification", "address", "city", "state",
        "zip", "tin", "tin_type", "signature_present", "signature_date",
    )


# ---------------------------------------------------------------------------
# Internal: supplier master
# ---------------------------------------------------------------------------
class SupplierRecord(BaseModel):
    supplier_id: str
    legal_name: str
    dba_name: Optional[str] = None
    tin_fingerprint: Optional[str] = None   # tenant-keyed HMAC blind index (never raw TIN)
    tin_last4: Optional[str] = None
    tax_classification: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    bank_account_ref: Optional[str] = None  # vault reference only
    status: str = "ACTIVE"
    last_updated: Optional[str] = None


class ValidationFlag(BaseModel):
    code: str
    severity: Severity
    field: Optional[str] = None
    message: str


class MatchScores(BaseModel):
    tin_exact: bool
    legal_name: float
    dba_name: float
    address: float
    composite: float


class MatchCandidate(BaseModel):
    supplier_id: str
    legal_name: str
    tin_relation: str = Field(..., description="SAME | DIFFERENT | MISSING_IN_MASTER | MISSING_ON_FORM")
    scores: MatchScores
    band: MatchBand = Field(..., description="Band of the name+address composite alone (ignores TIN). The decision combines this with tin_relation.")


class FieldChange(BaseModel):
    field: str
    current: Optional[str]
    proposed: Optional[str]
    material: bool


class MatchResult(BaseModel):
    strategy: str = Field(..., description="TIN_EXACT | FUZZY | NONE")
    best_candidate: Optional[MatchCandidate] = None
    candidates: list[MatchCandidate] = Field(default_factory=list, description="Top-N, best first.")
    candidates_considered: int = 0
    change_set: list[FieldChange] = Field(default_factory=list, description="Diff vs best candidate when a match is proposed.")


class SuggestedAction(BaseModel):
    """For HUMAN_REVIEW: what the engine would have done, to pre-fill the reviewer's screen."""
    decision: Decision
    supplier_id: Optional[str] = None
    confidence: float


class ReviewReason(BaseModel):
    code: str
    message: str


def parse_iso_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None
