"""Command-line entry point.

  python src/main.py process samples/documents/02_existing_refresh.pdf --tenant tenant_acme_mfg
  python src/main.py batch   samples/documents --manifest samples/ground_truth.json --out outputs/
  python src/main.py schema  --out schemas/onboarding_response.v1.json

JSON response -> stdout (or --out). Structured logs -> stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from w9_onboarding import config
config.load_env()   # GROQ_API_KEY / LLAMA_CLOUD_API_KEY from a git-ignored .env (optional)

from w9_onboarding.dto.onboarding_response import OnboardingResponse
from w9_onboarding.utils.observability import METRICS
from w9_onboarding.services.pipeline import OnboardingPipeline


def _dump(resp: OnboardingResponse) -> str:
    return resp.model_dump_json(indent=2)


def cmd_process(a: argparse.Namespace) -> int:
    p = OnboardingPipeline(a.tenant, extractor=a.extractor, ocr_provider=a.ocr, today=a.as_of)
    resp = p.process(a.file)
    if a.out:
        Path(a.out).write_text(_dump(resp), encoding="utf-8")
        print(f"{resp.decision.value} (confidence {resp.confidence:.2f}) -> {a.out}", file=sys.stderr)
    else:
        print(_dump(resp))
    return 0


def cmd_batch(a: argparse.Namespace) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8")) if a.manifest else None
    jobs = ([(Path(a.folder) / m["file"], m["tenant_id"]) for m in manifest] if manifest
            else [(f, a.tenant) for f in sorted(Path(a.folder).iterdir()) if f.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg")])
    pipes: dict[str, OnboardingPipeline] = {}
    print(f"{'document':38} {'tenant':16} {'decision':26} {'conf':>5}  supplier / review reasons")
    for path, tenant in jobs:
        pipe = pipes.setdefault(tenant, OnboardingPipeline(tenant, extractor=a.extractor, ocr_provider=a.ocr, today=a.as_of))
        resp = pipe.process(path)
        (out / f"{path.stem}.json").write_text(_dump(resp), encoding="utf-8")
        detail = resp.matched_supplier_id or ", ".join(r.code for r in resp.review_reasons) or "-"
        print(f"{path.name:38} {tenant:16} {resp.decision.value:26} {resp.confidence:5.2f}  {detail}")
    (out / "_metrics.json").write_text(json.dumps(METRICS.snapshot(), indent=2), encoding="utf-8")
    print(f"\nResponses and _metrics.json written to {out}/")
    return 0


def cmd_schema(a: argparse.Namespace) -> int:
    schema = OnboardingResponse.model_json_schema()
    text = json.dumps(schema, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"schema -> {a.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="W-9 supplier onboarding prototype")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--extractor", default="auto", choices=["auto", "layout", "llm", "hybrid"],
                       help="auto = hybrid if LLM credentials are set, else layout")
        p.add_argument("--ocr", default="auto", choices=["auto", "pdf-text", "llamaparse", "rapidocr"])
        p.add_argument("--as-of", type=date.fromisoformat, default=None, help="processing date (for reproducible runs)")

    p = sub.add_parser("process", help="process one document")
    p.add_argument("file")
    p.add_argument("--tenant", required=True)
    p.add_argument("--out")
    common(p)
    p.set_defaults(fn=cmd_process)

    b = sub.add_parser("batch", help="process a folder")
    b.add_argument("folder")
    b.add_argument("--manifest", help="ground_truth.json with per-file tenant ids")
    b.add_argument("--tenant", default="tenant_acme_mfg", help="tenant for all files when no manifest")
    b.add_argument("--out", default="outputs")
    common(b)
    b.set_defaults(fn=cmd_batch)

    s = sub.add_parser("schema", help="emit the response JSON schema")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_schema)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
