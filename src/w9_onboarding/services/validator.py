"""Validation layer: deterministic business rules over extracted fields.

Two jobs:
 1. Emit ValidationFlags (code, severity, field, message). Any BLOCKING flag forces HUMAN_REVIEW.
 2. Feed back into confidence: a value that fails a format rule cannot be "95% confident".

Rules are plain Python on purpose - auditors and product owners must be able to read them,
and they must behave identically for every tenant and every model version.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from w9_onboarding.config import POLICY, Policy
from w9_onboarding.domain.models import ExtractedW9, Severity, ValidationFlag, parse_iso_date
from w9_onboarding.utils.normalize import US_STATES

# IRS campus / online EIN prefixes that are actually issued (others are invalid).
VALID_EIN_PREFIXES = {
    *range(1, 7), *range(10, 17), *range(20, 28), *range(30, 49), *range(50, 69), *range(71, 78),
    *range(80, 89), *range(90, 96), 98, 99,
}

RULES = {  # code: (severity, description) - also rendered into the design doc
    "FORM_TYPE_NOT_W9": (Severity.BLOCKING, "Document is not a W-9 (e.g. W-8BEN from a foreign supplier)."),
    "MISSING_LEGAL_NAME": (Severity.BLOCKING, "Line 1 legal name is required for TIN matching."),
    "MISSING_TIN": (Severity.BLOCKING, "No TIN in Part I."),
    "INVALID_TIN_FORMAT": (Severity.BLOCKING, "TIN is not 9 digits."),
    "INVALID_EIN_PREFIX": (Severity.BLOCKING, "EIN prefix is not one the IRS issues."),
    "INVALID_SSN": (Severity.BLOCKING, "SSN area/group/serial is structurally invalid."),
    "TIN_TYPE_CLASSIFICATION_MISMATCH": (Severity.WARNING, "SSN supplied for an entity type that requires an EIN."),
    "MISSING_TAX_CLASSIFICATION": (Severity.BLOCKING, "No federal tax classification box checked."),
    "MULTIPLE_TAX_CLASSIFICATIONS": (Severity.BLOCKING, "More than one classification box checked."),
    "LLC_MISSING_SUBCLASS": (Severity.WARNING, "LLC checked without C/S/P letter."),
    "MISSING_SIGNATURE": (Severity.BLOCKING, "Part II not signed - form is invalid for tax reporting."),
    "SECOND_READER_UNAVAILABLE": (Severity.WARNING, "LLM second reader failed; result is from the deterministic reader alone."),
    "MISSING_SIGNATURE_DATE": (Severity.WARNING, "Signature present but undated."),
    "SIGNATURE_DATE_IN_FUTURE": (Severity.BLOCKING, "Signature date is after the processing date."),
    "SIGNATURE_DATE_STALE": (Severity.WARNING, "Signature older than policy window."),
    "MISSING_ADDRESS": (Severity.BLOCKING, "Street address (line 5) missing."),
    "MISSING_CITY_STATE_ZIP": (Severity.BLOCKING, "City/state/ZIP (line 6) missing or unparseable."),
    "INVALID_STATE": (Severity.BLOCKING, "Not a US state/territory code."),
    "INVALID_ZIP": (Severity.BLOCKING, "ZIP is not 5 or 9 digits."),
    "PO_BOX_ADDRESS": (Severity.INFO, "Address is a PO Box."),
    "OLD_FORM_REVISION": (Severity.INFO, "Older W-9 revision (accepted by IRS, but note for audit)."),
    "LOW_CONFIDENCE_CRITICAL_FIELD": (Severity.BLOCKING, "A critical field is below the automation confidence threshold."),
    "LOW_CONFIDENCE_FIELD": (Severity.WARNING, "A non-critical field is below the confidence threshold."),
    "EXTRACTOR_DISAGREEMENT": (Severity.WARNING, "Independent extractors read different values."),
    "UNGROUNDED_VALUE": (Severity.WARNING, "LLM value not found in OCR text (possible hallucination)."),
}


def _flag(code: str, field: Optional[str], detail: str = "") -> ValidationFlag:
    sev, desc = RULES[code]
    return ValidationFlag(code=code, severity=sev, field=field, message=f"{desc} {detail}".strip())


def validate_tin(tin: Optional[str], tin_type: Optional[str]) -> list[ValidationFlag]:
    if not tin:
        return [_flag("MISSING_TIN", "tin")]
    if not re.fullmatch(r"\d{9}", tin):
        return [_flag("INVALID_TIN_FORMAT", "tin")]
    if tin_type == "SSN":
        area, group, serial = int(tin[:3]), int(tin[3:5]), int(tin[5:])
        if area in (0, 666) or area >= 900 or group == 0 or serial == 0:
            return [_flag("INVALID_SSN", "tin")]
    else:  # EIN (or unknown -> treat as EIN, the common case for suppliers)
        if int(tin[:2]) not in VALID_EIN_PREFIXES:
            return [_flag("INVALID_EIN_PREFIX", "tin", f"(prefix {tin[:2]})")]
    return []


def validate(x: ExtractedW9, today: Optional[date] = None, policy: Policy = POLICY) -> tuple[ExtractedW9, list[ValidationFlag]]:
    """Return (confidence-adjusted copy of x, flags)."""
    today = today or date.today()
    x = x.model_copy(deep=True)
    flags: list[ValidationFlag] = []

    if x.form_type.value != "W-9":
        flags.append(_flag("FORM_TYPE_NOT_W9", "form_type", f"(detected: {x.form_type.value})"))
        return x, flags  # nothing else is meaningful on the wrong form

    if "SECOND_READER_UNAVAILABLE" in x.warnings:
        flags.append(_flag("SECOND_READER_UNAVAILABLE", None))

    if x.form_revision.value and not str(x.form_revision.value).startswith("2024"):
        flags.append(_flag("OLD_FORM_REVISION", "form_revision", f"({x.form_revision.value})"))

    # --- identity ---
    if not x.legal_name.value:
        flags.append(_flag("MISSING_LEGAL_NAME", "legal_name"))
    tin_flags = validate_tin(x.tin.value, x.tin_type.value)
    flags += tin_flags
    if tin_flags and x.tin.value:
        x.tin.confidence = min(x.tin.confidence, 0.3)  # format-invalid can't be high confidence

    # --- classification ---
    cls = x.tax_classification.value
    if cls is None:
        flags.append(_flag("MISSING_TAX_CLASSIFICATION", "tax_classification"))
    elif cls == "MULTIPLE":
        flags.append(_flag("MULTIPLE_TAX_CLASSIFICATIONS", "tax_classification"))
    elif cls == "LLC_UNSPECIFIED":
        flags.append(_flag("LLC_MISSING_SUBCLASS", "tax_classification"))
    if x.tin_type.value == "SSN" and cls in ("C_CORPORATION", "S_CORPORATION", "PARTNERSHIP", "LLC_C", "LLC_S", "LLC_P"):
        flags.append(_flag("TIN_TYPE_CLASSIFICATION_MISMATCH", "tin", f"(classification {cls})"))

    # --- certification ---
    if x.signature_present.value is not True:
        flags.append(_flag("MISSING_SIGNATURE", "signature_present"))
    sd = parse_iso_date(x.signature_date.value)
    if x.signature_present.value is True and sd is None:
        flags.append(_flag("MISSING_SIGNATURE_DATE", "signature_date"))
    if sd and sd > today:
        flags.append(_flag("SIGNATURE_DATE_IN_FUTURE", "signature_date", f"({sd.isoformat()})"))
        x.signature_date.confidence = min(x.signature_date.confidence, 0.4)
    if sd and (today - sd).days > policy.signature_max_age_days:
        flags.append(_flag("SIGNATURE_DATE_STALE", "signature_date", f"({(today - sd).days} days old)"))

    # --- address ---
    if not x.address.value:
        flags.append(_flag("MISSING_ADDRESS", "address"))
    elif re.search(r"\bp\.?\s*o\.?\s*box\b", str(x.address.value), re.I):
        flags.append(_flag("PO_BOX_ADDRESS", "address"))
    if not (x.city.value and x.state.value and x.zip.value):
        flags.append(_flag("MISSING_CITY_STATE_ZIP", "city"))
    else:
        if x.state.value not in US_STATES:
            flags.append(_flag("INVALID_STATE", "state", f"({x.state.value})"))
            x.state.confidence = min(x.state.confidence, 0.3)
        if not re.fullmatch(r"\d{5}(-\d{4})?", str(x.zip.value)):
            flags.append(_flag("INVALID_ZIP", "zip"))
            x.zip.confidence = min(x.zip.confidence, 0.3)

    # --- extraction-quality signals ---
    already_blocked = {fl.field for fl in flags if fl.severity == Severity.BLOCKING}
    for f in ExtractedW9.FIELD_NAMES:
        fc = getattr(x, f)
        if any(n.startswith("EXTRACTOR_DISAGREEMENT") for n in fc.notes):
            flags.append(_flag("EXTRACTOR_DISAGREEMENT", f))
        if any(n.startswith("UNGROUNDED") for n in fc.notes):
            flags.append(_flag("UNGROUNDED_VALUE", f))
        if fc.value is None or f in already_blocked:
            continue  # missing / invalid values are already reported by the specific rules above
        critical = f in policy.confidence.critical_fields
        threshold = policy.confidence.critical_field_min if critical else policy.confidence.standard_field_min
        if fc.confidence < threshold:
            code = "LOW_CONFIDENCE_CRITICAL_FIELD" if critical else "LOW_CONFIDENCE_FIELD"
            flags.append(_flag(code, f, f"({fc.confidence:.2f} < {threshold:.2f})"))
    return x, flags
