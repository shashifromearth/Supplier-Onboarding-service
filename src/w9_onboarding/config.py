"""Central configuration: thresholds and policy knobs.

Every number that changes a business decision lives here so it can be reviewed,
versioned, and (in production) overridden per tenant from a config store.
Nothing in this file is secret; secrets come from the environment / Key Vault.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent         # src/w9_onboarding
PROMPTS_DIR = PACKAGE_DIR / "prompts"
REPO_ROOT = PACKAGE_DIR.parents[1]                      # data (samples, calibration, .env) lives at the repo root

POLICY_VERSION = "2026-10-06.1"
SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class MatchThresholds:
    # Composite score bands (0-100) used when there is NO exact TIN match.
    auto_match: float = 95.0          # >= : same entity (still subject to TIN-conflict rules)
    review: float = 80.0              # [80, 95) : ambiguous -> human review
    # < review : treat as a new supplier

    # When the TIN DOES match, the name only has to corroborate (names drift:
    # "Acme Corp" vs "ACME CORPORATION INC").  Below this -> TIN/name conflict.
    tin_match_name_corroboration: float = 85.0

    # Legacy master rows without a TIN: allow auto-update only if name AND address agree.
    tin_backfill_min_address: float = 90.0

    # Two candidates within this many points of each other above the review band
    # = ambiguous (the master itself likely contains duplicates).
    ambiguity_margin: float = 3.0

    # Weights for the composite (no-TIN) score.
    w_name: float = 0.70
    w_address: float = 0.30


@dataclass(frozen=True)
class ConfidenceThresholds:
    # Per-field confidence below which a field is not trusted for automation.
    critical_field_min: float = 0.85     # legal_name, tin, signature_present
    standard_field_min: float = 0.70     # everything else
    critical_fields: tuple[str, ...] = ("legal_name", "tin", "signature_present", "tax_classification")


@dataclass(frozen=True)
class Policy:
    match: MatchThresholds = field(default_factory=MatchThresholds)
    confidence: ConfidenceThresholds = field(default_factory=ConfidenceThresholds)
    # Which changes on an existing supplier are "material" (force human review).
    material_change_fields: tuple[str, ...] = ("legal_name", "tin", "tax_classification")
    # Address changes are common and legitimate; flag them but do not block.
    review_on_address_change: bool = False
    signature_max_age_days: int = 3 * 365


POLICY = Policy()

# --- Secrets (dev defaults are deliberately obvious; never used outside local runs) ---
DEV_MASTER_KEY = "dev-only-not-a-secret-change-me"


def tin_hmac_master_key() -> bytes:
    """In production: fetched from Azure Key Vault (per-tenant key, HSM-backed)."""
    return os.environ.get("TIN_HMAC_MASTER_KEY", DEV_MASTER_KEY).encode()


# --- Rough unit prices for the per-document cost estimate (USD). UNVERIFIED: confirm against the
# vendors' current price pages before quoting; override via env without a code change. ---
def _price(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


PRICE_LLAMAPARSE_PER_CREDIT = _price("LLAMAPARSE_USD_PER_CREDIT", 0.00125)   # credits/page depend on tier
# (input, output) USD per 1M tokens by LLM provider. UNVERIFIED defaults: gpt-oss-20b on Groq; gemini-2.5-flash.
LLM_PRICES_PER_1M = {
    "groq": (_price("GROQ_INPUT_USD_PER_1M", 0.075), _price("GROQ_OUTPUT_USD_PER_1M", 0.30)),
    "gemini": (_price("GEMINI_INPUT_USD_PER_1M", 0.30), _price("GEMINI_OUTPUT_USD_PER_1M", 2.50)),
}


def load_env(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Never overrides variables already set in the environment.
    Keys live only in the environment or a git-ignored .env, never in code or docs."""
    for candidate in (Path(path), REPO_ROOT / ".env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
            return
