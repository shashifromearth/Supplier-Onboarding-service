"""Gemini-first / Groq-fallback behaviour. Fake SDK clients only: no network, no keys."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from w9_onboarding import config
from w9_onboarding.infrastructure.llm_clients import GeminiClient, GroqClient, LLMCallError, clients_from_env
from w9_onboarding.services.extraction.llm_extractor import LLMChain, LLMExtractionError, LLMExtractor
from w9_onboarding.domain.models import OcrLine, OcrResult
from w9_onboarding.services.pipeline import OnboardingPipeline, estimate_cost

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["legal_name", "dba_name", "tax_classification", "address", "city", "state", "zip",
          "tin_box", "signature_present", "signature_date"]


def _json(name="Acme Inc"):
    import json
    f = {k: {"value": None, "source_line_ids": [], "certainty": "low"} for k in FIELDS}
    f["legal_name"] = {"value": name, "source_line_ids": ["L0"], "certainty": "high"}
    return json.dumps({"form_type": "W-9", "issues": [], "fields": f})


def _ocr():
    return OcrResult(provider="t", lines=[OcrLine(text="Acme Inc", bbox=(0, 0, 10, 10), confidence=0.95)], page_count=1, dpi=200)


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class FakeGemini:
    """Mimics openai.OpenAI(...).chat.completions.create"""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))],
                               usage=SimpleNamespace(prompt_tokens=700, completion_tokens=300))


class FakeGroq:
    """Mimics openai.OpenAI(...).responses.create"""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(output_text=r, usage=SimpleNamespace(input_tokens=500, output_tokens=120))


def _chain(gemini_replies, groq_replies):
    g, q = FakeGemini(gemini_replies), FakeGroq(groq_replies)
    chain = LLMChain([LLMExtractor(GeminiClient(client=g)), LLMExtractor(GroqClient(client=q))])
    return chain, g, q


def test_gemini_is_used_first_and_groq_is_not_called():
    chain, g, q = _chain([_json()], [_json("never used")])
    x = chain.extract(_ocr())
    assert x.llm_provider == "gemini" and x.legal_name.value == "Acme Inc" and not q.calls
    assert g.calls[0]["messages"][0]["role"] == "system" and x.llm_usage == {"prompt_tokens": 700, "completion_tokens": 300}


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_bad_gemini_status_falls_back_to_groq(status):
    err = HttpError(status)
    chain, g, q = _chain([err, err] if status in (400, 422) else [err], [_json("From Groq")])
    x = chain.extract(_ocr())
    assert x.llm_provider == "groq" and x.legal_name.value == "From Groq" and len(q.calls) == 1


def test_gemini_unusable_json_falls_back_to_groq():
    chain, g, q = _chain(["not json", "still not json"], [_json()])
    assert chain.extract(_ocr()).llm_provider == "groq" and len(g.calls) == 2   # one retry, then fall back


def test_gemini_rejecting_reasoning_argument_is_retried_without_it():
    chain, g, _ = _chain([HttpError(400), _json()], [])
    assert chain.extract(_ocr()).llm_provider == "gemini"
    assert "reasoning_effort" in g.calls[0] and "reasoning_effort" not in g.calls[1]


def test_all_providers_failing_raises_and_hybrid_degrades_with_warning():
    from w9_onboarding.services.extraction.extractor import HybridExtractor
    chain, _, _ = _chain([HttpError(500)], [HttpError(503)])
    with pytest.raises(LLMExtractionError):
        chain.extract(_ocr())
    chain, _, _ = _chain([HttpError(500)], [HttpError(503)])
    pipe = OnboardingPipeline("tenant_acme_mfg", extractor=HybridExtractor(llm=chain), today=date(2026, 10, 6))
    resp = pipe.process(ROOT / "samples" / "documents" / "01_new_supplier_clean.pdf")
    assert resp.decision.value == "CREATE_NEW_SUPPLIER" and "SECOND_READER_UNAVAILABLE" in {f.code for f in resp.validation_flags}


def test_client_error_is_wrapped_with_status_code():
    with pytest.raises(LLMCallError) as e:
        GroqClient(client=FakeGroq([HttpError(429)])).complete("s", "u")
    assert e.value.status_code == 429 and e.value.provider == "groq"


def test_clients_from_env_order_and_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert clients_from_env() == [] and not LLMChain.available()
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert [c.provider for c in clients_from_env()] == ["groq"]
    monkeypatch.setenv("GEMINI_API_KEY", "y")
    assert [c.provider for c in clients_from_env()] == ["gemini", "groq"]        # Gemini first
    assert LLMChain.from_env().name == "llm-chain[gemini>groq]"


def test_cost_uses_the_provider_that_answered():
    chain, _, _ = _chain([_json()], [])
    x = chain.extract(_ocr())
    p_in, p_out = config.LLM_PRICES_PER_1M["gemini"]
    assert estimate_cost(None, x) == pytest.approx(700 / 1e6 * p_in + 300 / 1e6 * p_out)
