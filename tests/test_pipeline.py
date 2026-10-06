"""End-to-end and security tests over the sample documents."""
import json
from datetime import date
from pathlib import Path

import pytest

from w9_onboarding.infrastructure.llm_clients import GroqClient
from w9_onboarding.services.extraction.llm_extractor import LLMExtractor, llm_input_lines, parse_llm_json
from w9_onboarding.utils.normalize import tin_from_text
from w9_onboarding.domain.models import OcrLine, OcrResult
from w9_onboarding.utils.observability import get_logger
from w9_onboarding.services.pipeline import OnboardingPipeline

ROOT = Path(__file__).resolve().parents[1]
TRUTH = json.loads((ROOT / "samples" / "ground_truth.json").read_text(encoding="utf-8"))
PDFS = [t for t in TRUTH if t["file"].endswith(".pdf")]  # fast (text layer); images run in eval/


@pytest.fixture(scope="module")
def pipes():
    return {}


@pytest.mark.parametrize("case", PDFS, ids=[t["file"] for t in PDFS])
def test_sample_decisions(case, pipes):
    pipe = pipes.setdefault(case["tenant_id"], OnboardingPipeline(case["tenant_id"], extractor="layout", today=date(2026, 10, 6)))
    resp = pipe.process(ROOT / "samples" / "documents" / case["file"])
    assert resp.decision.value == case["expected"]["decision"]
    assert resp.matched_supplier_id == case["expected"].get("supplier_id")
    if case["expected"].get("review_code"):
        assert case["expected"]["review_code"] in {r.code for r in resp.review_reasons}


def test_response_never_contains_plain_tin(pipes):
    case = TRUTH[1]
    resp = OnboardingPipeline(case["tenant_id"], extractor="layout").process(ROOT / "samples" / "documents" / case["file"])
    body = resp.model_dump_json()
    assert case["fields"]["tin"] not in body
    assert "36-4817265" not in body
    assert resp.extracted_fields.tin.masked == "**-***7265"


def test_logs_redact_tin_shaped_values(capsys):
    log = get_logger("w9.test")
    log.info("supplier sent 36-4817265 and 523-81-4470")
    err = capsys.readouterr().err
    assert "4817265" not in err and "523-81-4470" not in err and "REDACTED" in err


def test_unreadable_file_returns_review(tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not really a pdf")
    resp = OnboardingPipeline("tenant_acme_mfg", extractor="layout").process(bad)
    assert resp.decision.value == "HUMAN_REVIEW"
    assert resp.review_reasons[0].code == "DOCUMENT_UNREADABLE"


# ---------------------------------------------------------------- LLM path (no API call)
def _ocr(lines):
    return OcrResult(provider="test", lines=[OcrLine(text=t, bbox=(0, i * 20, 100, i * 20 + 10), confidence=0.95) for i, t in enumerate(lines)],
                     page_count=1, dpi=200)


def test_tin_is_redacted_before_llm():
    numbered = llm_input_lines(_ocr(["Employer identification number", "36-4817265", "Acme Inc"]))
    texts = [l.text for _, l in numbered]
    assert "36-4817265" not in " ".join(texts) and "[TIN REDACTED]" in texts


def test_tin_is_redacted_from_markdown_before_llm():
    o = _ocr([])
    o.markdown = "| EIN | 36-4817265 |\nSSN 523-81-4470 here\n| --- | --- |\nAcme Inc"
    joined = " ".join(l.text for _, l in llm_input_lines(o))
    assert "4817265" not in joined and "523-81-4470" not in joined and "Acme Inc" in joined


class _StubClient:
    provider, model = "stub", "m"

    def complete(self, system, user):
        raise AssertionError("no network in unit tests")


def test_llm_grounding_penalises_hallucinated_values():
    ocr = _ocr(["1 Name of entity/individual", "Acme Industrial Supply, Inc.", "5 Address", "1200 W Lake St"])
    by_id = {f"L{i}": l for i, l in enumerate(ocr.lines)}
    item = lambda v, ids, c="high": {"value": v, "source_line_ids": ids, "certainty": c}  # noqa: E731
    payload = {"form_type": "W-9", "issues": [], "fields": {
        "legal_name": item("Acme Industrial Supply, Inc.", ["L1"]),
        "address": item("999 Invented Blvd", ["L3"]),          # not what L3 says
        "dba_name": item(None, []), "tax_classification": item("C_CORPORATION", [], "medium"),
        "city": item(None, []), "state": item(None, []), "zip": item(None, []), "tin_box": item("EIN", []),
        "signature_present": item(True, [], "high"), "signature_date": item("09/02/2026", [])}}
    x = LLMExtractor(_StubClient()).from_payload(payload, by_id)
    assert x.legal_name.confidence > 0.9
    assert x.address.confidence < 0.5 and any("UNGROUNDED" in n for n in x.address.notes)
    assert x.signature_date.value == "2026-09-02"


def test_hybrid_reconciliation_agreement_and_disagreement():
    """Hybrid = layout + LLM. Stub the LLM so the reconciliation logic is tested offline."""
    from w9_onboarding.services.extraction.extractor import HybridExtractor
    from w9_onboarding.infrastructure.ocr import load_document, run_ocr

    ocr = run_ocr(load_document(ROOT / "samples" / "documents" / "02_existing_refresh.pdf"))

    class StubLLM:
        name = "stub-llm"

        def extract(self, _ocr):
            x = HybridExtractor().layout.extract(_ocr)  # start from identical reading...
            x.address = x.address.model_copy(update={"value": "1200 W Lake St Ste 9", "source": "stub-llm"})  # ...one disagreement
            x.legal_name = x.legal_name.model_copy(update={"confidence": 0.8, "source": "stub-llm"})
            return x

    out = HybridExtractor(llm=StubLLM()).extract(ocr)
    assert out.legal_name.confidence >= 0.98   # agreement boosts confidence above either extractor alone
    assert out.address.confidence < 0.7                                                   # disagreement penalised
    assert any("EXTRACTOR_DISAGREEMENT" in n for n in out.address.notes)


# ---------------------------------------------------------------- Groq / LlamaParse paths (fakes, no network)
from types import SimpleNamespace  # noqa: E402

GOOD_JSON = ('{"form_type":"W-9","issues":[],"fields":{"legal_name":{"value":"Acme Inc","source_line_ids":["L0"],"certainty":"high"},'
             '"dba_name":{"value":null,"source_line_ids":[],"certainty":"high"},"tax_classification":{"value":null,"source_line_ids":[],"certainty":"low"},'
             '"address":{"value":"1 Main St","source_line_ids":["L1"],"certainty":"high"},"city":{"value":null,"source_line_ids":[],"certainty":"low"},'
             '"state":{"value":null,"source_line_ids":[],"certainty":"low"},"zip":{"value":null,"source_line_ids":[],"certainty":"low"},'
             '"tin_box":{"value":"EIN","source_line_ids":[],"certainty":"medium"},"signature_present":{"value":null,"source_line_ids":[],"certainty":"low"},'
             '"signature_date":{"value":null,"source_line_ids":[],"certainty":"low"}}}')


class FakeGroq:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(output_text=self.replies.pop(0), usage=SimpleNamespace(input_tokens=500, output_tokens=120))


@pytest.mark.parametrize("raw", [GOOD_JSON, "```json\n" + GOOD_JSON + "\n```", "Here you go:\n" + GOOD_JSON + "\nDone."])
def test_llm_json_parse_variants(raw):
    assert parse_llm_json(raw).fields["legal_name"].value == "Acme Inc"


def test_llm_retries_once_on_invalid_json_then_succeeds():
    fake = FakeGroq(["not json", GOOD_JSON])
    x = LLMExtractor(GroqClient(client=fake)).extract(_ocr(["Acme Inc", "1 Main St"]))
    assert len(fake.calls) == 2 and x.legal_name.value == "Acme Inc" and x.llm_usage["prompt_tokens"] == 1000
    assert "image" not in str(fake.calls[0]).lower() or "cannot see" in fake.calls[0]["instructions"].lower()


def test_llm_raises_after_two_invalid_replies():
    from w9_onboarding.services.extraction.llm_extractor import LLMExtractionError
    with pytest.raises(LLMExtractionError):
        LLMExtractor(GroqClient(client=FakeGroq(["nope", "still nope"]))).extract(_ocr(["Acme Inc"]))


def test_text_only_llm_abstains_on_visual_fields():
    x = LLMExtractor(GroqClient(client=FakeGroq([GOOD_JSON]))).extract(_ocr(["Acme Inc", "1 Main St"]))
    assert x.signature_present.value is None and any("NOT_ASSESSED" in n for n in x.signature_present.notes)
    assert x.tax_classification.value is None


def test_tin_text_fallback():
    assert tin_from_text("EIN 36-4817265")[:2] == ("364817265", "EIN")
    assert tin_from_text("SSN 523-81-4470")[:2] == ("523814470", "SSN")
    assert tin_from_text("nothing here") is None
    assert tin_from_text("36-4817265 and 12-3456789")[2] == 0.5


def test_hybrid_degrades_to_layout_when_llm_fails():
    from w9_onboarding.services.extraction.extractor import HybridExtractor
    from w9_onboarding.infrastructure.ocr import load_document, run_ocr

    class Boom:
        def extract(self, _):
            raise RuntimeError("network down")

    ocr = run_ocr(load_document(ROOT / "samples" / "documents" / "02_existing_refresh.pdf"))
    out = HybridExtractor(llm=Boom()).extract(ocr)
    assert "SECOND_READER_UNAVAILABLE" in out.warnings and out.legal_name.value


def test_pipeline_with_fake_llm_failure_flags_warning_not_crash():
    from w9_onboarding.services.extraction.extractor import HybridExtractor

    class Boom:
        def extract(self, _):
            raise RuntimeError("down")

    pipe = OnboardingPipeline("tenant_acme_mfg", extractor=HybridExtractor(llm=Boom()), today=date(2026, 10, 6))
    resp = pipe.process(ROOT / "samples" / "documents" / "01_new_supplier_clean.pdf")
    assert resp.decision.value == "CREATE_NEW_SUPPLIER"
    assert "SECOND_READER_UNAVAILABLE" in {f.code for f in resp.validation_flags}


# LlamaParse (fake client, no network)
class FakeLlama:
    def __init__(self, result, fail_first_expand=False):
        self.result, self.expands = result, []
        self.files = SimpleNamespace(create=lambda **kw: SimpleNamespace(id="file_1"))
        self.parsing = SimpleNamespace(parse=self._parse)
        self.fail_first_expand = fail_first_expand

    def _parse(self, **kw):
        self.expands.append(kw["expand"])
        if self.fail_first_expand and len(self.expands) == 1:
            err = Exception("bad expand")
            err.status_code = 422
            raise err
        return self.result


def _llama_result(markdown, credits=10):
    return SimpleNamespace(markdown_full=markdown, items=None, metadata=None,
                           job=SimpleNamespace(status="COMPLETED", usage=SimpleNamespace(credits=credits)))


def test_llamaparse_uses_markdown_cost_and_local_geometry():
    from w9_onboarding.infrastructure.ocr import LLAMAPARSE_USD_PER_CREDIT, llamaparse, load_document
    doc = load_document(ROOT / "samples" / "documents" / "02_existing_refresh.pdf")
    fake = FakeLlama(_llama_result("# W-9\nAcme Industrial Supply, Inc.", credits=10), fail_first_expand=True)
    out = llamaparse(doc, client=fake)
    assert out.markdown.startswith("# W-9") and out.lines                      # geometry falls back to local lines
    assert out.provider.startswith("llamaparse:agentic") and "local-geometry" in out.provider
    assert out.cost_usd == pytest.approx(10 * LLAMAPARSE_USD_PER_CREDIT)
    assert len(fake.expands) == 2 and len(fake.expands[1]) < len(fake.expands[0])   # retried with fewer expand fields


def test_llamaparse_failed_job_raises_ocr_error():
    from w9_onboarding.infrastructure.ocr import OcrError, llamaparse, load_document
    doc = load_document(ROOT / "samples" / "documents" / "02_existing_refresh.pdf")
    bad = SimpleNamespace(markdown_full="", items=None, metadata=None, job=SimpleNamespace(status="FAILED", error_message="boom"))
    with pytest.raises(OcrError):
        llamaparse(doc, client=FakeLlama(bad))


def test_full_pipeline_with_fake_llamaparse_and_fake_groq(monkeypatch):
    """Both vendors faked: markdown goes to the LLM (TIN redacted), cost = credits + tokens."""
    from w9_onboarding.infrastructure import ocr as o
    from w9_onboarding.services.extraction.extractor import HybridExtractor
    doc_path = ROOT / "samples" / "documents" / "02_existing_refresh.pdf"
    real = o.load_document(doc_path)
    fake_llama = FakeLlama(_llama_result("Name: Acme Industrial Supply, Inc.\nEIN 36-4817265", credits=10))
    monkeypatch.setattr(o, "run_ocr", lambda d, provider="auto": o.llamaparse(d, client=fake_llama))
    from w9_onboarding.services import pipeline as pl
    monkeypatch.setattr(pl, "run_ocr", o.run_ocr)
    fake_groq = FakeGroq([GOOD_JSON])
    pipe = OnboardingPipeline("tenant_acme_mfg", extractor=HybridExtractor(llm=LLMExtractor(GroqClient(client=fake_groq))), today=date(2026, 10, 6))
    resp = pipe.process(doc_path)
    sent = str(fake_groq.calls[0])
    assert "4817265" not in sent and "TIN REDACTED" in sent                        # redacted before leaving
    assert real.sha256 and resp.processing.estimated_cost_usd > 10 * o.LLAMAPARSE_USD_PER_CREDIT * 0.99
    assert "36-4817265" not in resp.model_dump_json()


# Regression for the first live run on Windows: files are read with the locale codec (cp1252) unless told otherwise,
# and the prompts contain non-ASCII characters.
def test_every_text_file_io_is_explicit_utf8():
    import re
    bad = []
    for f in ROOT.rglob("*.py"):
        if ".venv" in f.parts or "__pycache__" in f.parts or f.name == "test_pipeline.py":
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.read_text\(\)", line) or (re.search(r"\.write_text\(", line) and "encoding=" not in line):
                bad.append(f"{f.name}:{n}")
    assert not bad, bad
