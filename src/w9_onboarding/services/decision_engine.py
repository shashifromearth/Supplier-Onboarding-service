"""Decision engine: (validated extraction, match result) -> CREATE / UPDATE / HUMAN_REVIEW.

An ordered, first-match-wins rule list. Every rule appends a human-readable line to
`reasoning` and, when it routes to review, a machine-readable `ReviewReason` code.

Philosophy: automation must be *earned*. We auto-create or auto-update only when
the document is valid, critical fields are confidently read, and the identity evidence is
unambiguous. Everything else goes to a human with a pre-filled suggestion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from w9_onboarding.config import POLICY, Policy
from w9_onboarding.domain.models import (Decision, ExtractedW9, MatchCandidate, MatchResult, ReviewReason, Severity, 
                    SuggestedAction, ValidationFlag)


@dataclass
class DecisionOutcome:
    decision: Decision
    confidence: float
    matched_supplier_id: Optional[str] = None
    suggested_action: Optional[SuggestedAction] = None
    review_reasons: list[ReviewReason] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)


def _extraction_confidence(x: ExtractedW9, policy: Policy) -> float:
    vals = [getattr(x, f).confidence for f in policy.confidence.critical_fields]
    return min(vals) if vals else 0.0


def _create_certainty(best: Optional[MatchCandidate], policy: Policy) -> float:
    """How sure are we nothing in the master is this supplier? 1.0 if nothing resembles it."""
    if best is None:
        return 0.99
    top = policy.match.review
    s = best.scores.composite
    return round(max(0.5, min(0.99, (top - s) / (top - 40))), 3)


def decide(x: ExtractedW9, flags: list[ValidationFlag], m: MatchResult, policy: Policy = POLICY) -> DecisionOutcome:
    t = policy.match
    why: list[str] = []
    reasons: list[ReviewReason] = []
    ext_conf = _extraction_confidence(x, policy)
    best = m.best_candidate
    cands = m.candidates

    # ---- Step 1: propose an action purely from identity evidence -------------------------
    proposed: Decision
    target: Optional[str] = None
    match_conf: float
    review_code: Optional[tuple[str, str]] = None

    if x.form_type.value == "UNREADABLE":
        proposed, match_conf = Decision.HUMAN_REVIEW, 0.0
        review_code = ("DOCUMENT_UNREADABLE", "Document could not be opened or read: " + "; ".join(x.form_type.notes))
        why.append("Document unreadable; nothing extracted.")
    elif x.form_type.value != "W-9":
        proposed, match_conf = Decision.HUMAN_REVIEW, 0.0
        review_code = ("FORM_TYPE_NOT_W9", f"Document is a {x.form_type.value}, not a W-9; request the correct form.")
        why.append(f"Form detected as {x.form_type.value}; W-9 matching not attempted.")
    elif best is not None and best.scores.tin_exact:
        tin_hits = [c for c in cands if c.scores.tin_exact]
        name = max(best.scores.legal_name, best.scores.dba_name)
        why.append(f"TIN matches existing supplier {best.supplier_id} (fingerprint equality); name similarity {name:.0f}.")
        if len(tin_hits) > 1:
            proposed, target, match_conf = Decision.UPDATE_EXISTING_SUPPLIER, best.supplier_id, 0.5
            review_code = ("DUPLICATE_TIN_IN_MASTER", f"{len(tin_hits)} existing suppliers share this TIN: "
                           + ", ".join(c.supplier_id for c in tin_hits) + ". Master needs de-duplication.")
        elif name >= t.tin_match_name_corroboration:
            proposed, target = Decision.UPDATE_EXISTING_SUPPLIER, best.supplier_id
            match_conf = 0.90 + 0.09 * (name / 100)
            material = [c for c in m.change_set if c.material]
            if material:
                review_code = ("MATERIAL_CHANGE", "Material change on existing supplier: "
                               + ", ".join(f"{c.field} '{c.current}' -> '{c.proposed}'" for c in material))
            elif m.change_set:
                fields = ", ".join(c.field for c in m.change_set)
                why.append(f"Non-material changes vs master: {fields}.")
                if policy.review_on_address_change and any(c.field in ("address", "city", "state", "zip") for c in m.change_set):
                    review_code = ("ADDRESS_CHANGE", "Policy requires review of address changes.")
            else:
                why.append("No field changes vs master: W-9 refresh (new certification date on file).")
        else:
            proposed, target, match_conf = Decision.UPDATE_EXISTING_SUPPLIER, best.supplier_id, 0.4
            review_code = ("TIN_NAME_CONFLICT", f"TIN is on file for '{best.legal_name}' but the form names "
                           f"'{x.legal_name.value}' (similarity {name:.0f} < {t.tin_match_name_corroboration:.0f}). "
                           "Legitimate restructure or payment-redirection attempt - verify with supplier.")
    elif best is not None and best.band != "NO_MATCH" or (best is not None and best.tin_relation == "MISSING_IN_MASTER"
                                                           and max(best.scores.legal_name, best.scores.dba_name) >= 90):
        s = best.scores
        name = max(s.legal_name, s.dba_name)
        why.append(f"No TIN match. Best fuzzy candidate {best.supplier_id} '{best.legal_name}': "
                   f"name {name:.0f}, address {s.address:.0f}, composite {s.composite:.0f}, TIN {best.tin_relation}.")
        close = [c for c in cands if c is not best and c.scores.composite >= t.review
                 and best.scores.composite - c.scores.composite <= t.ambiguity_margin]
        if best.tin_relation == "DIFFERENT":
            if s.composite >= 90:
                proposed, match_conf = Decision.CREATE_NEW_SUPPLIER, 0.5
                review_code = ("NAME_MATCH_TIN_CONFLICT", f"Looks like existing supplier {best.supplier_id} by name/address "
                               f"(composite {s.composite:.0f}) but the TIN differs. Possible duplicate, new entity, or impostor.")
            else:
                proposed, match_conf = Decision.CREATE_NEW_SUPPLIER, _create_certainty(best, policy) * 0.9
                why.append("Candidate has a different TIN on file and only partial similarity: treated as a different legal entity.")
        elif close:
            proposed, target, match_conf = Decision.UPDATE_EXISTING_SUPPLIER, best.supplier_id, 0.45
            review_code = ("AMBIGUOUS_CANDIDATES", "Multiple similar suppliers: "
                           + ", ".join(f"{c.supplier_id} ({c.scores.composite:.0f})" for c in [best, *close]))
        elif best.tin_relation == "MISSING_IN_MASTER" and s.composite >= t.auto_match and s.address >= t.tin_backfill_min_address:
            proposed, target = Decision.UPDATE_EXISTING_SUPPLIER, best.supplier_id
            match_conf = 0.85 * s.composite / 100
            why.append("Legacy record has no TIN; name and address both corroborate -> update and backfill TIN.")
        else:
            proposed, target, match_conf = Decision.UPDATE_EXISTING_SUPPLIER, best.supplier_id, round(s.composite / 100 * 0.7, 3)
            review_code = ("FUZZY_MATCH_REVIEW_BAND", f"Possible match {best.supplier_id} '{best.legal_name}' "
                           f"(name {name:.0f}, address {s.address:.0f}) with no TIN to confirm or rule it out.")
    else:
        proposed = Decision.CREATE_NEW_SUPPLIER
        match_conf = _create_certainty(best, policy)
        why.append("No TIN match and no candidate above the review threshold "
                   + (f"(best: {best.supplier_id} composite {best.scores.composite:.0f})." if best else "(no candidates)."))

    if review_code:
        reasons.append(ReviewReason(code=review_code[0], message=review_code[1]))

    # ---- Step 2: document validity gates automation ---------------------------------------
    blocking = [f for f in flags if f.severity == Severity.BLOCKING]
    for f in blocking:
        if f.code != "FORM_TYPE_NOT_W9":
            reasons.append(ReviewReason(code=f.code, message=f.message))
    if blocking:
        why.append(f"{len(blocking)} blocking validation issue(s): " + ", ".join(sorted({f.code for f in blocking})) + ".")
    warnings = [f.code for f in flags if f.severity == Severity.WARNING]
    if warnings:
        why.append("Warnings (non-blocking): " + ", ".join(sorted(set(warnings))) + ".")

    # ---- Step 3: combine ------------------------------------------------------------------
    conf = round(max(0.0, min(0.99, ext_conf * match_conf)), 3)
    why.append(f"Critical-field extraction confidence {ext_conf:.2f}; identity confidence {match_conf:.2f}.")
    if reasons:
        why.append(f"Routed to human review; suggested action: {proposed.value}" + (f" ({target})." if target else "."))
        suggestion = None if proposed == Decision.HUMAN_REVIEW else SuggestedAction(decision=proposed, supplier_id=target, confidence=conf)
        return DecisionOutcome(Decision.HUMAN_REVIEW, conf, None, suggestion, reasons, why)
    why.append(f"Automated decision: {proposed.value}" + (f" -> {target}." if target else "."))
    return DecisionOutcome(proposed, conf, target if proposed == Decision.UPDATE_EXISTING_SUPPLIER else None, None, [], why)
