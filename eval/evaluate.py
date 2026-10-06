"""Evaluation harness.

  python eval/evaluate.py                    # clean sample set
  python eval/evaluate.py --variant scan     # re-render every PDF as a degraded scan -> forces the OCR path

Measures
  * Field-level precision / recall / accuracy (extraction)
  * Confidence calibration (is a 0.9 field right ~90% of the time?)
  * Decision accuracy + confusion matrix
  * Match precision, false-match rate, duplicate-creation rate, human-review rate
  * Name-matcher precision/recall vs threshold on labelled pairs (eval/name_pairs.csv)

Writes eval/report_<variant>.md. Ground truth: samples/ground_truth.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import logging  # noqa: E402

import pandas as pd  # noqa: E402

from w9_onboarding.services.matcher import name_similarity  # noqa: E402
from w9_onboarding.utils.normalize import squash  # noqa: E402
from w9_onboarding.services.pipeline import OnboardingPipeline  # noqa: E402

FIELDS = ["legal_name", "dba_name", "tax_classification", "address", "city", "state", "zip", "tin", "tin_type",
          "signature_present", "signature_date"]


def _eq(field: str, pred, truth) -> bool:
    if pred is None or truth is None:
        return pred is None and truth is None
    if isinstance(truth, bool):
        return pred == truth
    return squash(str(pred)) == squash(str(truth))


def predicted_fields(resp, pipe: OnboardingPipeline) -> dict:
    ef = resp.extracted_fields
    out = {f: getattr(ef, f).value for f in FIELDS if f not in ("tin", "tin_type")}
    out["tin_type"] = ef.tin.tin_type.value if ef.tin.tin_type.value != "UNKNOWN" else None
    # Ground truth holds the synthetic TIN; compare via the vault (as an authorised evaluator would).
    out["tin"] = None
    if ef.tin.vault_ref:
        out["tin"] = pipe.vault.reveal(resp.tenant_id, ef.tin.vault_ref)
    conf = {f: getattr(ef, f).confidence for f in FIELDS if f not in ("tin", "tin_type")}
    conf["tin"] = conf["tin_type"] = ef.tin.confidence
    return out, conf


def make_scan_variants(truth: list[dict], workdir: Path) -> dict[str, Path]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from generate_samples import pdf_to_image
    paths = {}
    for t in truth:
        src = ROOT / "samples" / "documents" / t["file"]
        if src.suffix == ".pdf":
            dst = workdir / (src.stem + "_scan.jpg")
            pdf_to_image(src, dst, dpi=150, photo=True)
            paths[t["file"]] = dst
        else:
            paths[t["file"]] = src
    return paths


def name_threshold_table() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "eval" / "name_pairs.csv")
    df["score"] = [name_similarity(a, b) for a, b in zip(df.name_a, df.name_b)]
    rows = []
    for th in (70, 80, 85, 90, 95):
        pred = df.score >= th
        tp = int((pred & (df.same_entity == 1)).sum())
        fp = int((pred & (df.same_entity == 0)).sum())
        fn = int((~pred & (df.same_entity == 1)).sum())
        rows.append({"threshold": th, "precision": tp / (tp + fp) if tp + fp else 1.0, "recall": tp / (tp + fn) if tp + fn else 1.0,
                     "false_positives": fp, "false_negatives": fn})
    return pd.DataFrame(rows), df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["clean", "scan"], default="clean")
    ap.add_argument("--extractor", default="layout")
    ap.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 6))
    a = ap.parse_args()
    logging.getLogger("w9.pipeline").disabled = True

    truth = json.loads((ROOT / "samples" / "ground_truth.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp())
    paths = make_scan_variants(truth, tmp) if a.variant == "scan" else {t["file"]: ROOT / "samples" / "documents" / t["file"] for t in truth}

    pipes: dict[str, OnboardingPipeline] = {}
    stats = defaultdict(Counter)
    calib: list[tuple[float, bool]] = []
    decisions = []
    for t in truth:
        pipe = pipes.setdefault(t["tenant_id"], OnboardingPipeline(t["tenant_id"], extractor=a.extractor, today=a.as_of))
        resp = pipe.process(paths[t["file"]])
        pred, conf = predicted_fields(resp, pipe)
        if t["expected"].get("review_code") != "FORM_TYPE_NOT_W9":  # field metrics only for real W-9s
            for f in FIELDS:
                p, g = pred[f], t["fields"][f]
                ok = _eq(f, p, g)
                stats[f]["n"] += 1
                stats[f]["correct"] += ok
                if p is not None:
                    stats[f]["tp" if ok else "fp"] += 1
                if g is not None and not ok:
                    stats[f]["fn"] += 1
                if p is not None:
                    calib.append((conf[f], ok))
        exp = t["expected"]
        decisions.append({
            "file": t["file"], "expected": exp["decision"], "predicted": resp.decision.value,
            "expected_supplier": exp.get("supplier_id"), "predicted_supplier": resp.matched_supplier_id,
            "expected_review_code": exp.get("review_code"),
            "review_codes": ",".join(r.code for r in resp.review_reasons), "confidence": resp.confidence,
        })

    # ---- extraction ----
    frows = []
    for f in FIELDS:
        s = stats[f]
        prec = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else 1.0
        rec = s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else 1.0
        frows.append({"field": f, "accuracy": s["correct"] / s["n"], "precision": prec, "recall": rec, "n": s["n"]})
    fdf = pd.DataFrame(frows)

    # ---- calibration ----
    bins = [(0.0, 0.7), (0.7, 0.85), (0.85, 0.95), (0.95, 1.01)]
    crow = []
    for lo, hi in bins:
        sel = [ok for c, ok in calib if lo <= c < hi]
        crow.append({"confidence_bin": f"[{lo:.2f},{min(hi, 1):.2f})", "n": len(sel),
                     "observed_accuracy": (sum(sel) / len(sel)) if sel else None})
    cdf = pd.DataFrame(crow)

    # ---- decisions ----
    ddf = pd.DataFrame(decisions)
    ddf["predicted_supplier"] = ddf.predicted_supplier.fillna("-")
    ddf["expected_supplier"] = ddf.expected_supplier.fillna("-")
    ddf["decision_ok"] = ddf.expected == ddf.predicted
    ddf["supplier_ok"] = ddf.expected_supplier == ddf.predicted_supplier
    ddf["review_code_ok"] = [(not isinstance(e, str)) or (e in (c or "")) for e, c in zip(ddf.expected_review_code, ddf.review_codes)]
    auto_upd = ddf[ddf.predicted == "UPDATE_EXISTING_SUPPLIER"]
    false_match = auto_upd[(auto_upd.expected != "UPDATE_EXISTING_SUPPLIER") | (~auto_upd.supplier_ok)]
    auto_new = ddf[ddf.predicted == "CREATE_NEW_SUPPLIER"]
    dup_created = auto_new[auto_new.expected == "UPDATE_EXISTING_SUPPLIER"]
    gt_upd = ddf[ddf.expected == "UPDATE_EXISTING_SUPPLIER"]
    kpis = {
        "documents": len(ddf),
        "decision_accuracy": ddf.decision_ok.mean(),
        "review_reason_accuracy": ddf.review_code_ok.mean(),
        "human_review_rate": (ddf.predicted == "HUMAN_REVIEW").mean(),
        "expected_human_review_rate": (ddf.expected == "HUMAN_REVIEW").mean(),
        "match_precision (auto-updates to the right supplier)": (1 - len(false_match) / len(auto_upd)) if len(auto_upd) else 1.0,
        "match_recall (true updates automated)": (len(auto_upd[(auto_upd.expected == "UPDATE_EXISTING_SUPPLIER") & auto_upd.supplier_ok]) / len(gt_upd)) if len(gt_upd) else 1.0,
        "false_match_rate (wrong auto-update / auto-updates)": (len(false_match) / len(auto_upd)) if len(auto_upd) else 0.0,
        "duplicate_creation_rate (auto-create of existing supplier)": (len(dup_created) / len(auto_new)) if len(auto_new) else 0.0,
        "field_accuracy_micro": fdf.accuracy.mean(),
    }
    confusion = pd.crosstab(ddf.expected, ddf.predicted, rownames=["expected"], colnames=["predicted"])
    ndf, pairs = name_threshold_table()

    def md(df: pd.DataFrame) -> str:
        return df.to_markdown(index=False, floatfmt=".3f") if hasattr(df, "to_markdown") else df.to_string()

    try:
        import tabulate  # noqa: F401
    except ImportError:
        md = lambda df: "```\n" + df.to_string(index=False) + "\n```"  # noqa: E731

    report = [f"# Evaluation report - variant `{a.variant}`, extractor `{a.extractor}`\n",
              "## KPIs\n", "\n".join(f"- **{k}**: {v:.3f}" if isinstance(v, float) else f"- **{k}**: {v}" for k, v in kpis.items()),
              "\n## Field-level extraction\n", md(fdf),
              "\n## Confidence calibration (all non-null predicted fields)\n", md(cdf),
              "\n## Decision confusion matrix\n", "```\n" + confusion.to_string() + "\n```",
              "\n## Per-document decisions\n", md(ddf[["file", "expected", "predicted", "predicted_supplier", "review_codes", "confidence", "decision_ok"]]),
              "\n## Name matcher: precision/recall vs threshold (eval/name_pairs.csv, n=%d)\n" % len(pairs), md(ndf),
              "\nMisclassified pairs at 90:\n", md(pairs[(pairs.score >= 90) != (pairs.same_entity == 1)][["name_a", "name_b", "same_entity", "score", "note"]])]
    text = "\n".join(report)
    out = ROOT / "eval" / f"report_{a.variant}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
