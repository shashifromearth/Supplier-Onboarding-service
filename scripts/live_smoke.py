"""Live smoke test for LlamaParse + Groq. Run it yourself with your own keys:

    export LLAMA_CLOUD_API_KEY=...  GEMINI_API_KEY=...  GROQ_API_KEY=...   (or a git-ignored .env; Gemini is tried first, Groq is the fallback)
    python scripts/live_smoke.py samples/documents/09_phone_photo_existing.jpg

Prints which env vars are present (never their values), a markdown preview, which geometry source was
used, masked extracted fields, the decision, latency and estimated cost. Spends a few LlamaParse credits.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from w9_onboarding import config  # noqa: E402

config.load_env()

from w9_onboarding.infrastructure.ocr import load_document, run_ocr  # noqa: E402
from w9_onboarding.services.pipeline import OnboardingPipeline  # noqa: E402


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "samples/documents/09_phone_photo_existing.jpg")
    tenant = sys.argv[2] if len(sys.argv) > 2 else "tenant_acme_mfg"
    for k in ("LLAMA_CLOUD_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY"):
        print(f"{k}: {'set' if os.environ.get(k) else 'MISSING'}")
    if not (os.environ.get("LLAMA_CLOUD_API_KEY") and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"))):
        return 2

    t = time.perf_counter()
    ocr = run_ocr(load_document(path), "llamaparse")
    print(f"\n[ocr] provider={ocr.provider} lines={len(ocr.lines)} page_conf={ocr.page_confidence} "
          f"cost=${ocr.cost_usd:.4f} ({time.perf_counter() - t:.1f}s)")
    print("[markdown preview]\n" + "\n".join((ocr.markdown or "(none)").splitlines()[:25]))
    if ocr.lines:
        print("[first lines + bbox]", [(l.text[:25], tuple(int(v) for v in l.bbox)) for l in ocr.lines[:4]],
              f"| image {ocr.page_images[0].size}  <- bbox should sit inside the image")

    pipe = OnboardingPipeline(tenant, extractor="hybrid", ocr_provider="llamaparse")
    resp = pipe.process(path)
    print("\n[decision]", resp.decision.value, f"confidence={resp.confidence:.2f}", "supplier=", resp.matched_supplier_id)
    print("[extractor]", resp.processing.extractor, "| est. cost $%.4f" % resp.processing.estimated_cost_usd)
    for name, f in resp.extracted_fields.model_dump().items():
        if isinstance(f, dict) and "value" in f or isinstance(f, dict) and "masked" in f:
            print(f"  {name:20} {f.get('masked') or f.get('value')!s:40} conf={f.get('confidence')}")
    print("[flags]", [f.code for f in resp.validation_flags], "[review]", [r.code for r in resp.review_reasons])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
