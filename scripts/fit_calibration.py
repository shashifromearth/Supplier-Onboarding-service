"""Fit the OCR confidence calibration map on a *separate* synthetic calibration set.

Generates N random W-9s (random names/addresses/classifications, varied scan degradation),
runs OCR + layout extraction WITHOUT calibration, records (raw confidence, correct?) per
field, fits a monotone map and writes calibration/<provider>.json.

  python scripts/fit_calibration.py --n 60
"""
from __future__ import annotations

import argparse
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from w9_onboarding.services.calibration import CALIBRATED_FIELDS, OCR_DERIVED_SOURCES, _key, fit, save  # noqa: E402
from w9_onboarding.services.extraction.layout_extractor import LayoutExtractor  # noqa: E402
from generate_samples import make_pdf, pdf_to_image  # noqa: E402
from w9_onboarding.utils.normalize import squash  # noqa: E402
from w9_onboarding.infrastructure.ocr import load_document, run_ocr  # noqa: E402

W1 = ["Granite", "Harbor", "Maple", "Copper", "Silver", "Prairie", "Cedar", "Atlas", "Beacon", "Crescent", "Falcon",
      "Juniper", "Keystone", "Liberty", "Meridian", "Northstar", "Orchard", "Pioneer", "Quarry", "Redwood", "Sterling"]
W2 = ["Logistics", "Fabrication", "Analytics", "Supply", "Electric", "Builders", "Foods", "Medical", "Printing",
      "Staffing", "Software", "Plumbing", "Freight", "Design", "Packaging", "Tooling", "Labs", "Energy"]
SUF = ["Inc", "LLC", "Corp", "Co", "LP", "Ltd", "Corporation", "Company", ""]
STREETS = ["Main St", "Oak Ave", "Industrial Pkwy", "Commerce Dr", "Market Street", "Elm Rd", "River Rd", "Airport Blvd"]
CITIES = [("Dallas", "TX", "75201"), ("Denver", "CO", "80202"), ("Columbus", "OH", "43215"), ("Raleigh", "NC", "27601"),
          ("Omaha", "NE", "68102"), ("Boise", "ID", "83702"), ("Tampa", "FL", "33602"), ("St. Louis", "MO", "63101"),
          ("Salt Lake City", "UT", "84101"), ("Albany", "NY", "12207")]
CLASSES = ["C_CORPORATION", "S_CORPORATION", "PARTNERSHIP", "LLC_C", "LLC_S", "LLC_P", "INDIVIDUAL_SOLE_PROPRIETOR"]
EIN_PREFIXES = [10, 20, 26, 27, 30, 35, 41, 45, 52, 56, 61, 74, 81, 84, 88, 91, 94, 98]


def random_w9(rnd: random.Random) -> dict:
    cls = rnd.choice(CLASSES)
    city, st, z = rnd.choice(CITIES)
    if cls == "INDIVIDUAL_SOLE_PROPRIETOR":
        tin = f"{rnd.randint(100, 665):03d}{rnd.randint(10, 99):02d}{rnd.randint(1000, 9999):04d}"
        tin_type, disp = "SSN", f"{tin[:3]}-{tin[3:5]}-{tin[5:]}"
        legal = f"{rnd.choice(['Ana', 'Ben', 'Chen', 'Dev', 'Eli', 'Fatima'])} {rnd.choice(['Ortiz', 'Patel', 'Kim', 'Novak', 'Reyes'])}"
        dba = f"{rnd.choice(W1)} {rnd.choice(W2)}"
    else:
        tin = f"{rnd.choice(EIN_PREFIXES):02d}{rnd.randint(1000000, 9999999)}"
        tin_type, disp = "EIN", f"{tin[:2]}-{tin[2:]}"
        legal = f"{rnd.choice(W1)} {rnd.choice(W2)} {rnd.choice(SUF)}".strip()
        dba = rnd.choice([None, None, f"{rnd.choice(W1)} {rnd.choice(W2)}"])
    m, d = rnd.randint(1, 9), rnd.randint(1, 28)
    return dict(legal_name=legal, dba_name=dba, tax_classification=cls,
                address=f"{rnd.randint(1, 9999)} {rnd.choice(STREETS)}" + rnd.choice(["", "", f" Ste {rnd.randint(100, 900)}"]),
                city=city, state=st, zip=z, tin=tin, tin_type=tin_type, tin_display=disp,
                signature_date=f"2026-{m:02d}-{d:02d}", signature_date_display=f"{m:02d}/{d:02d}/2026")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--ocr", default="auto", choices=["auto", "llamaparse", "rapidocr"],
                    help="llamaparse needs LLAMA_CLOUD_API_KEY and costs credits per page")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    tmp = Path(tempfile.mkdtemp())
    points: list[tuple[float, bool]] = []
    provider = None
    for i in range(a.n):
        d = random_w9(rnd)
        pdf = tmp / f"cal_{i}.pdf"
        make_pdf(pdf, d, rev=rnd.choice(["2024", "2024", "2018"]), signed=True, seed=i)
        img = tmp / f"cal_{i}.jpg"
        pdf_to_image(pdf, img, dpi=rnd.choice([130, 150, 170, 200]), photo=True,
                     rotate=rnd.uniform(-1.2, 1.2), blur=rnd.uniform(0.3, 1.0), seed=i)
        ocr = run_ocr(load_document(img), a.ocr)
        provider = ocr.provider
        x = LayoutExtractor(calibrated=False).extract(ocr)
        for f in CALIBRATED_FIELDS:
            fc = getattr(x, f)
            if fc.value is None or not fc.source.startswith(OCR_DERIVED_SOURCES):
                continue
            truth = d[f] if f != "signature_date" else d["signature_date"]
            ok = truth is not None and squash(str(fc.value)) == squash(str(truth))
            points.append((fc.confidence, ok))
        if (i + 1) % 10 == 0:
            print(f"{i + 1}/{a.n} documents, {len(points)} field observations", file=sys.stderr)
    pts_path = ROOT / "calibration" / f"{_key(provider)}_points.csv"
    pts_path.parent.mkdir(exist_ok=True)
    pts_path.write_text("raw_confidence,correct\n" + "\n".join(f"{c:.4f},{int(ok)}" for c, ok in points), encoding="utf-8")
    bins = fit(points)
    acc = sum(ok for _, ok in points) / len(points)
    path = save(provider, bins, {"n_documents": a.n, "n_fields": len(points), "raw_accuracy": round(acc, 4),
                                 "seed": a.seed, "note": "synthetic bootstrap; refit on reviewed production data"})
    print(f"overall field accuracy on calibration set: {acc:.3f}")
    print("raw-confidence bin -> calibrated P(correct)  [n]")
    for lo, hi, p, n in bins:
        print(f"  [{lo:.2f},{hi:.2f})  ->  {p:.3f}   [{n}]")
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
