"""Confidence calibration.

Raw OCR confidences are not probabilities: RapidOCR reports ~0.80 on text it reads
correctly almost every time (see eval/report_scan.md before calibration). Thresholds are a
*policy* ("auto-approve if P(correct) >= 0.85"), so the number fed to them must be a
probability. We fit a monotone (isotonic, binned) map per OCR provider:

    raw confidence -> observed accuracy on a labelled calibration set

Calibration set != test set. The prototype bootstraps it with synthetic scans
(scripts/fit_calibration.py); production refits weekly from human-review outcomes,
per provider and per extractor version, and alerts when the map drifts (DESIGN.md Part 4).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from w9_onboarding.config import REPO_ROOT
from w9_onboarding.domain.models import ExtractedW9

CAL_DIR = REPO_ROOT / "calibration"
# Only fields whose confidence is derived from OCR text confidence are calibrated by this map.
OCR_DERIVED_SOURCES = ("layout:value-below-label", "layout:tin-box", "layout:date", "layout:csz-pattern")
CALIBRATED_FIELDS = ("legal_name", "dba_name", "address", "city", "state", "zip", "tin", "tin_type", "signature_date")
EDGES = [0.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.01]


def fit(points: Iterable[tuple[float, bool]], prior_strength: float = 5.0, cap: float = 0.97) -> list[list[float]]:
    """Binned isotonic regression with a Beta prior (so empty/small bins stay conservative)."""
    pts = list(points)
    bins = []
    for lo, hi in zip(EDGES, EDGES[1:]):
        sel = [ok for c, ok in pts if lo <= c < hi]
        # prior centred on the bin's raw midpoint: no evidence => keep raw confidence
        mid = (lo + min(hi, 1.0)) / 2
        p = (sum(sel) + prior_strength * mid) / (len(sel) + prior_strength)
        bins.append([lo, min(hi, 1.0), p, len(sel)])
    # enforce monotonicity (pool adjacent violators, weighted by count)
    changed = True
    while changed:
        changed = False
        for i in range(len(bins) - 1):
            if bins[i][2] > bins[i + 1][2]:
                n1, n2 = bins[i][3] + 1, bins[i + 1][3] + 1
                pooled = (bins[i][2] * n1 + bins[i + 1][2] * n2) / (n1 + n2)
                bins[i][2] = bins[i + 1][2] = pooled
                changed = True
    # cap: synthetic calibration data is cleaner than production; never claim near-certainty from OCR alone
    return [[round(lo, 2), round(hi, 2), round(min(p, cap), 4), n] for lo, hi, p, n in bins]


def _key(provider: str) -> str:
    """File-safe calibration key: drop the geometry suffix and any character illegal in filenames
    ("llamaparse:agentic(items-bbox)" -> "llamaparse-agentic")."""
    import re as _re
    if "local-geometry" in provider:      # positions/confidences came from local OCR, so use that provider's map
        return "rapidocr-onnx"
    return _re.sub(r"[^A-Za-z0-9_.-]+", "-", provider.split("(")[0]).strip("-")


def save(provider: str, bins: list[list[float]], meta: dict) -> Path:
    CAL_DIR.mkdir(exist_ok=True)
    path = CAL_DIR / f"{_key(provider)}.json"
    path.write_text(json.dumps({"provider": provider, "bins": bins, **meta}, indent=2), encoding="utf-8")
    return path


_cache: dict[str, list | None] = {}


def _load(provider: str) -> list | None:
    if provider not in _cache:
        p = CAL_DIR / f"{_key(provider)}.json"
        _cache[provider] = json.loads(p.read_text(encoding="utf-8"))["bins"] if p.exists() else None
    return _cache[provider]


def calibrate(x: ExtractedW9, provider: str) -> ExtractedW9:
    bins = _load(provider)
    if not bins:
        return x
    x = x.model_copy(deep=True)
    for f in CALIBRATED_FIELDS:
        fc = getattr(x, f)
        if fc.value is None or not fc.source.startswith(OCR_DERIVED_SOURCES):
            continue
        for lo, hi, p, _ in bins:
            if lo <= fc.confidence < hi or (hi >= 1.0 and fc.confidence >= 1.0):
                fc.notes.append(f"calibrated {fc.confidence:.3f}->{p:.3f} ({provider})")
                fc.confidence = round(p, 3)
                break
    return x
