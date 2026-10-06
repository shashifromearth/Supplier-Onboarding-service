"""Directional unit tests for validation, matching and decision rules (no documents needed)."""
from datetime import date

import pytest

from w9_onboarding.services.decision_engine import decide
from w9_onboarding.services.matcher import Matcher, name_similarity
from w9_onboarding.domain.models import Decision, Severity
from w9_onboarding.utils.security import mask_tin, tin_fingerprint
from w9_onboarding.repositories.supplier_master import CsvSupplierMaster, TenantNotFound
from tests.helpers import fc, make_w9
from w9_onboarding.services.validator import validate

TODAY = date(2026, 10, 6)


def codes(flags):
    return {f.code for f in flags}


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("tin,ttype,expected", [
    ("074418290", "EIN", "INVALID_EIN_PREFIX"),   # 07 never issued
    ("004418290", "EIN", "INVALID_EIN_PREFIX"),
    ("66612345", "EIN", "INVALID_TIN_FORMAT"),     # 8 digits
    ("666123456", "SSN", "INVALID_SSN"),           # area 666
    ("123004567", "SSN", "INVALID_SSN"),           # group 00
])
def test_invalid_tins_block(tin, ttype, expected):
    _, flags = validate(make_w9(tin=tin, tin_type=ttype), today=TODAY)
    assert expected in codes(flags)
    assert any(f.severity == Severity.BLOCKING for f in flags)


def test_clean_form_has_no_blocking_flags():
    _, flags = validate(make_w9(), today=TODAY)
    assert not [f for f in flags if f.severity == Severity.BLOCKING]


def test_missing_signature_and_address_block():
    _, flags = validate(make_w9(signature_present=False, address=None, city=None), today=TODAY)
    assert {"MISSING_SIGNATURE", "MISSING_ADDRESS", "MISSING_CITY_STATE_ZIP"} <= codes(flags)


def test_signature_dates():
    _, flags = validate(make_w9(signature_date="2027-01-01"), today=TODAY)
    assert "SIGNATURE_DATE_IN_FUTURE" in codes(flags)
    _, flags = validate(make_w9(signature_date="2019-01-01"), today=TODAY)
    assert "SIGNATURE_DATE_STALE" in codes(flags)


def test_ssn_with_corporation_warns():
    _, flags = validate(make_w9(tin="523814470", tin_type="SSN", tax_classification="C_CORPORATION"), today=TODAY)
    assert "TIN_TYPE_CLASSIFICATION_MISMATCH" in codes(flags)


def test_low_confidence_critical_field_blocks():
    _, flags = validate(make_w9(legal_name=fc("Acme Industrial Supply, Inc.", 0.6)), today=TODAY)
    assert "LOW_CONFIDENCE_CRITICAL_FIELD" in codes(flags)


def test_wrong_form_short_circuits():
    _, flags = validate(make_w9(form_type=fc("W-8BEN")), today=TODAY)
    assert codes(flags) == {"FORM_TYPE_NOT_W9"}


# ---------------------------------------------------------------- matching
@pytest.mark.parametrize("a,b", [
    ("Acme Corporation", "Acme Corp"), ("Acme Corporation", "ACME CORPORATION INC"),
    ("Acme Corporation", "Acme, Corp."), ("Contoso Ltd.", "Contoso Limited"),
])
def test_name_variants_are_equivalent(a, b):
    assert name_similarity(a, b) >= 95


def test_shared_brand_token_is_not_a_match():
    assert name_similarity("Acme Industrial Supply Inc", "Acme Packaging Co") < 80


def test_fingerprints_are_tenant_scoped():
    assert tin_fingerprint("tenant_a", "364817265") != tin_fingerprint("tenant_b", "364817265")
    assert tin_fingerprint("tenant_a", "364817265") == tin_fingerprint("tenant_a", "364817265")


def test_tenant_id_cannot_traverse_paths():
    with pytest.raises(TenantNotFound):
        CsvSupplierMaster("../tenant_globex")


def test_masking():
    assert mask_tin("364817265", "EIN") == "**-***7265"
    assert mask_tin("523814470", "SSN") == "***-**-4470"


# ---------------------------------------------------------------- decisions
def run(x, tenant="tenant_acme_mfg"):
    x, flags = validate(x, today=TODAY)
    return decide(x, flags, Matcher(CsvSupplierMaster(tenant)).match(x))


def test_tin_match_updates_existing():
    out = run(make_w9())
    assert out.decision == Decision.UPDATE_EXISTING_SUPPLIER and out.matched_supplier_id == "SUP-1001"


def test_same_supplier_other_tenant_is_new():
    assert run(make_w9(), tenant="tenant_globex").decision == Decision.CREATE_NEW_SUPPLIER


def test_tin_on_file_under_other_name_goes_to_review():
    out = run(make_w9(legal_name="Totally Different Holdings LLC"))
    assert out.decision == Decision.HUMAN_REVIEW
    assert out.review_reasons[0].code == "TIN_NAME_CONFLICT"
    assert out.suggested_action and out.suggested_action.supplier_id == "SUP-1001"


def test_material_change_goes_to_review():
    out = run(make_w9(tax_classification="S_CORPORATION"))
    assert out.decision == Decision.HUMAN_REVIEW and out.review_reasons[0].code == "MATERIAL_CHANGE"


def test_blocking_validation_overrides_confident_match():
    out = run(make_w9(signature_present=False))
    assert out.decision == Decision.HUMAN_REVIEW
    assert out.suggested_action.decision == Decision.UPDATE_EXISTING_SUPPLIER


def test_master_duplicates_are_ambiguous():
    # SUP-1008 'Proseware Inc' and SUP-1009 'PROSEWARE, INC.' (no TIN) are duplicates in the master itself.
    x = make_w9(legal_name="Proseware Inc", tin="811170299", address="12 Harbor View Rd", city="Portland", state="ME", zip="04101")
    out = run(x)
    assert out.decision == Decision.HUMAN_REVIEW
