"""Matching layer: hybrid deterministic + fuzzy supplier matching (RapidFuzz).

Signal hierarchy (strongest first):
  1. TIN exact match (tenant-scoped HMAC fingerprint equality - no decryption needed)
  2. Legal-name similarity   (normalised, suffix-stripped; token_sort / token_set)
  3. DBA similarity          (DBA<->DBA and cross DBA<->legal, since suppliers swap them)
  4. Address similarity      (USPS-style abbreviation normalisation + ZIP/city agreement)

The matcher only *scores*. Turning scores into CREATE / UPDATE / REVIEW is the decision
engine's job, so thresholds can change without touching scoring code (and vice versa).
"""
from __future__ import annotations

from typing import Optional

from rapidfuzz import fuzz

from w9_onboarding.config import POLICY, Policy
from w9_onboarding.domain.models import (ExtractedW9, FieldChange, MatchBand, MatchCandidate, MatchResult, MatchScores, 
                    SupplierRecord)
from w9_onboarding.utils.normalize import normalize_address, normalize_name
from w9_onboarding.utils.security import tin_fingerprint
from w9_onboarding.repositories.supplier_master import SupplierMasterRepository


def name_similarity(a: Optional[str], b: Optional[str]) -> float:
    """0-100. 'Acme Corp' ~ 'ACME CORPORATION INC' = 100 (suffixes stripped before scoring).

    token_sort_ratio handles word order ("Toys Tailspin"); token_set_ratio handles one name being
    a subset of the other ("Northwind" vs "Northwind Traders") but is discounted, because subset
    matches are the main source of false positives ("Acme" is a subset of every "Acme ...").
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    sort = fuzz.token_sort_ratio(na, nb)
    shorter = min(len(na.split()), len(nb.split()))
    subset = fuzz.token_set_ratio(na, nb) * (0.90 if shorter >= 2 else 0.75)
    return round(max(sort, subset), 1)


def address_similarity(x: ExtractedW9, r: SupplierRecord) -> float:
    """0-100. Street similarity dominates; ZIP and city agreement corroborate."""
    if not x.address.value or not r.address:
        return 0.0
    street = fuzz.token_sort_ratio(normalize_address(x.address.value), normalize_address(r.address))
    zip_eq = 100.0 if (x.zip.value or "")[:5] == (r.zip or "")[:5] and r.zip else 0.0
    city_eq = 100.0 if (x.city.value or "").strip().lower() == (r.city or "").strip().lower() and r.city else 0.0
    return round(0.70 * street + 0.20 * zip_eq + 0.10 * city_eq, 1)


class Matcher:
    def __init__(self, repo: SupplierMasterRepository, policy: Policy = POLICY, top_n: int = 3) -> None:
        self.repo = repo
        self.policy = policy
        self.top_n = top_n

    def match(self, x: ExtractedW9) -> MatchResult:
        if x.form_type.value != "W-9" or not x.legal_name.value:
            return MatchResult(strategy="NONE")
        t = self.policy.match
        fp = tin_fingerprint(self.repo.tenant_id, x.tin.value) if x.tin.value else None

        tin_hits = self.repo.find_by_tin_fingerprint(fp) if fp else []
        fuzzy = self.repo.search_candidates(x.legal_name.value, x.dba_name.value, (x.zip.value or "")[:5] or None)
        pool = {r.supplier_id: r for r in [*tin_hits, *fuzzy]}

        scored: list[MatchCandidate] = []
        for r in pool.values():
            legal = name_similarity(x.legal_name.value, r.legal_name)
            dba = max(name_similarity(x.dba_name.value, r.dba_name),
                      name_similarity(x.dba_name.value, r.legal_name),
                      name_similarity(x.legal_name.value, r.dba_name)) if (x.dba_name.value or r.dba_name) else 0.0
            addr = address_similarity(x, r)
            composite = round(t.w_name * max(legal, dba) + t.w_address * addr, 1)
            if fp and r.tin_fingerprint:
                rel = "SAME" if r.tin_fingerprint == fp else "DIFFERENT"
            elif not fp:
                rel = "MISSING_ON_FORM"
            else:
                rel = "MISSING_IN_MASTER"
            band = (MatchBand.AUTO_MATCH if composite >= t.auto_match
                    else MatchBand.REVIEW if composite >= t.review else MatchBand.NO_MATCH)
            scored.append(MatchCandidate(
                supplier_id=r.supplier_id, legal_name=r.legal_name, tin_relation=rel,
                scores=MatchScores(tin_exact=rel == "SAME", legal_name=legal, dba_name=dba, address=addr, composite=composite),
                band=band))

        # TIN matches first, then by composite score.
        scored.sort(key=lambda c: (c.scores.tin_exact, c.scores.composite), reverse=True)
        best = scored[0] if scored else None
        strategy = "TIN_EXACT" if best and best.scores.tin_exact else ("FUZZY" if best and best.band != MatchBand.NO_MATCH else "NONE")
        result = MatchResult(strategy=strategy, best_candidate=best, candidates=scored[: self.top_n],
                             candidates_considered=len(scored))
        if best and (best.scores.tin_exact or best.band != MatchBand.NO_MATCH):
            result.change_set = self.diff(x, pool[best.supplier_id], best.tin_relation)
        return result

    def diff(self, x: ExtractedW9, r: SupplierRecord, tin_relation: str) -> list[FieldChange]:
        """What would change on the existing record. Cosmetic differences are not changes."""
        material = set(self.policy.material_change_fields)
        changes: list[FieldChange] = []

        def add(field: str, cur: Optional[str], new: Optional[str], same: bool) -> None:
            # Never propose overwriting known master data with "unknown/blank" from the form.
            if not same and new:
                changes.append(FieldChange(field=field, current=cur, proposed=new, material=field in material))

        add("legal_name", r.legal_name, x.legal_name.value, normalize_name(r.legal_name) == normalize_name(x.legal_name.value))
        add("dba_name", r.dba_name, x.dba_name.value, normalize_name(r.dba_name) == normalize_name(x.dba_name.value))
        cls_new = x.tax_classification.value
        cls_same = r.tax_classification == cls_new or (cls_new == "LLC_UNSPECIFIED" and (r.tax_classification or "").startswith("LLC"))
        add("tax_classification", r.tax_classification, cls_new, cls_same)
        add("address", r.address, x.address.value, normalize_address(r.address) == normalize_address(x.address.value))
        add("city", r.city, x.city.value, (r.city or "").lower() == (x.city.value or "").lower())
        add("state", r.state, x.state.value, r.state == x.state.value)
        add("zip", r.zip, x.zip.value, (r.zip or "")[:5] == (x.zip.value or "")[:5])
        if tin_relation == "MISSING_IN_MASTER":
            changes.append(FieldChange(field="tin", current=None, proposed=f"***{x.tin.value[-4:]}", material=False))
        elif tin_relation == "DIFFERENT":
            changes.append(FieldChange(field="tin", current=f"***{r.tin_last4}", proposed=f"***{x.tin.value[-4:]}", material=True))
        return changes
