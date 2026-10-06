"""Transport adapters for chat-style LLM APIs: one small class per provider, nothing W-9 specific.

  GeminiClient  Google Gemini via its OpenAI-compatible Chat Completions endpoint   (GEMINI_API_KEY)
  GroqClient    Groq-hosted gpt-oss-20b via the OpenAI-compatible Responses API     (GROQ_API_KEY)

Both reuse the `openai` SDK, so there is no extra dependency. Every failure (HTTP error status, timeout,
connection) is raised as LLMCallError so the caller can fall back to the next provider.
Keys are read from the environment only.
"""
from __future__ import annotations

import os
from typing import Optional, Protocol

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
TIMEOUT_S = 30.0
MAX_OUTPUT_TOKENS = 3000


class LLMCallError(Exception):
    def __init__(self, provider: str, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider, self.status_code = provider, status_code


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        """Return (text, {"prompt_tokens": n, "completion_tokens": n}). Raise LLMCallError on any failure."""
        ...


def _openai_client(provider: str, key_env: str, base_url: str):
    key = os.environ.get(key_env)
    if not key:
        raise LLMCallError(provider, f"{key_env} is not set")
    from openai import OpenAI
    return OpenAI(api_key=key, base_url=base_url, timeout=TIMEOUT_S, max_retries=1)


def _call(provider: str, fn, base_kwargs: dict, optional_kwargs: dict):
    """Call fn(**base, **optional); if the provider rejects the optional arguments (HTTP 400/422), retry without them."""
    try:
        try:
            return fn(**base_kwargs, **optional_kwargs)
        except Exception as e:
            if getattr(e, "status_code", None) in (400, 422) and optional_kwargs:
                return fn(**base_kwargs)
            raise
    except Exception as e:
        raise LLMCallError(provider, f"{type(e).__name__}: {str(e)[:160]}", getattr(e, "status_code", None)) from e


class GeminiClient:
    provider = "gemini"

    def __init__(self, model: Optional[str] = None, client=None) -> None:
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        self._client = client

    def complete(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        if self._client is None:
            self._client = _openai_client(self.provider, "GEMINI_API_KEY", os.environ.get("GEMINI_BASE_URL", GEMINI_BASE_URL))
        resp = _call(self.provider, self._client.chat.completions.create,
                     dict(model=self.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                          temperature=0, max_tokens=MAX_OUTPUT_TOKENS),
                     {"reasoning_effort": "low"})
        usage = getattr(resp, "usage", None)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        return text, {"prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                      "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0)}


class GroqClient:
    provider = "groq"

    def __init__(self, model: Optional[str] = None, client=None) -> None:
        self.model = model or os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)
        self._client = client

    def complete(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        if self._client is None:
            self._client = _openai_client(self.provider, "GROQ_API_KEY", os.environ.get("GROQ_BASE_URL", GROQ_BASE_URL))
        resp = _call(self.provider, self._client.responses.create,
                     dict(model=self.model, instructions=system, input=user, temperature=0, max_output_tokens=MAX_OUTPUT_TOKENS),
                     {"reasoning": {"effort": "low"}})
        usage = getattr(resp, "usage", None)
        return resp.output_text, {"prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                                  "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0)}


def clients_from_env() -> list[LLMClient]:
    """Providers in priority order, only those whose key is set: Gemini first, Groq as the fallback."""
    out: list[LLMClient] = []
    if os.environ.get("GEMINI_API_KEY"):
        out.append(GeminiClient())
    if os.environ.get("GROQ_API_KEY"):
        out.append(GroqClient())
    return out
