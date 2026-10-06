"""Diagnostic: show what each reader said per field, plus the parsed-text lines about classification.

    python scripts/diff_readers.py samples/documents/12_rev2018_tin_backfill.pdf [--ocr llamaparse|auto]

Uses your keys (one LlamaParse parse if the file needs it, one call per configured LLM provider). TIN-shaped strings are masked.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from w9_onboarding import config  # noqa: E402

config.load_env()

from w9_onboarding.services.extraction.layout_extractor import LayoutExtractor  # noqa: E402
from w9_onboarding.infrastructure.llm_clients import clients_from_env  # noqa: E402
from w9_onboarding.services.extraction.llm_extractor import LLMExtractor  # noqa: E402
from w9_onboarding.domain.models import ExtractedW9  # noqa: E402
from w9_onboarding.infrastructure.ocr import load_document, run_ocr  # noqa: E402
from w9_onboarding.utils.security import TIN_LIKE  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--ocr", default="auto", choices=["auto", "llamaparse", "rapidocr"])
    a = ap.parse_args()
    ocr = run_ocr(load_document(a.file), a.ocr)
    print(f"[ocr] {ocr.provider} lines={len(ocr.lines)} markdown={'yes' if ocr.markdown else 'no'}")
    lay = LayoutExtractor().extract(ocr)
    for client in clients_from_env():              # every configured provider, so you can compare Gemini and Groq
        try:
            llm = LLMExtractor(client).extract(ocr)
        except Exception as e:
            print(f"\n[{client.provider}] FAILED: {type(e).__name__}: {str(e)[:120]}")
            continue
        print(f"\n[{client.provider}:{client.model}] tokens={llm.llm_usage}")
        print(f"{'field':20} {'LAYOUT value (conf)':44} LLM value (conf)  [notes]")
        for f in ExtractedW9.FIELD_NAMES:
            if f in ("tin", "tin_type"):
                continue
            x, y = getattr(lay, f), getattr(llm, f)
            print(f"{f:20} {str(x.value)[:30]:30} ({x.confidence:.2f})   {str(y.value)[:30]:30} ({y.confidence:.2f})  {'; '.join(y.notes)[:90]}")
    print("\n[parsed lines about classification]")
    for line in ocr.full_text.splitlines():
        if re.search(r"LLC|limited liability|corporation|partnership|\[x\]|\[ \]|☒|☐|tax classification", line, re.I):
            print("  ", TIN_LIKE.sub("[TIN]", line)[:170])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
