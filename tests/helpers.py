"""Builders for synthetic ExtractedW9 objects so rules can be tested without documents."""
from w9_onboarding.domain.models import ExtractedW9, FieldCandidate


def fc(value, conf=0.98, source="test"):
    return FieldCandidate(value=value, confidence=conf, source=source)


def make_w9(**over) -> ExtractedW9:
    base = dict(
        form_type=fc("W-9"), form_revision=fc("2024-mar"), legal_name=fc("Acme Industrial Supply, Inc."),
        dba_name=fc(None, 0.9), tax_classification=fc("C_CORPORATION"), address=fc("1200 W Lake St"),
        city=fc("Chicago"), state=fc("IL"), zip=fc("60607"), tin=fc("364817265"), tin_type=fc("EIN"),
        signature_present=fc(True), signature_date=fc("2026-09-02"), extractor="test")
    for k, v in over.items():
        base[k] = v if isinstance(v, FieldCandidate) else fc(v)
    return ExtractedW9(**base)
