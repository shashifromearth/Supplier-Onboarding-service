"""TIN protection primitives.

Production mapping (see docs/DESIGN.md, Part 3):
  * tenant_key()      -> per-tenant key in Azure Key Vault (Managed HSM), never leaves KV in prod;
                         HMAC computed via KV "sign"/"wrapKey" or in a confidential compute enclave.
  * TinVault          -> Azure Key Vault-wrapped DEK + encrypted column in the tenant's
                         supplier store (or a dedicated tokenisation service).
  * fingerprint()     -> blind index so we can do equality matching on TINs without decrypting.
  * RedactingFilter   -> last line of defence: scrubs anything TIN-shaped from logs.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Optional

from w9_onboarding.config import tin_hmac_master_key

TIN_LIKE = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b|\b\d{2}-?\d{7}\b")


def tenant_key(tenant_id: str) -> bytes:
    """Derive a per-tenant key (HKDF-style). Different tenants => unlinkable fingerprints."""
    return hmac.new(tin_hmac_master_key(), f"tin-fingerprint|{tenant_id}".encode(), hashlib.sha256).digest()


def tin_fingerprint(tenant_id: str, tin_digits: str) -> str:
    """Tenant-scoped blind index. Same TIN in two tenants yields different values (isolation)."""
    digest = hmac.new(tenant_key(tenant_id), tin_digits.encode(), hashlib.sha256).hexdigest()
    return f"hmac256:{digest[:32]}"


def mask_tin(tin_digits: Optional[str], tin_type: str) -> Optional[str]:
    if not tin_digits or len(tin_digits) != 9:
        return None
    last4 = tin_digits[-4:]
    return f"***-**-{last4}" if tin_type == "SSN" else f"**-***{last4}"


class RedactingFilter(logging.Filter):
    """Scrub TIN-shaped substrings from every log record (defence in depth)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = TIN_LIKE.sub("[REDACTED-TIN]", record.msg)
        if record.args:
            record.args = tuple(TIN_LIKE.sub("[REDACTED-TIN]", a) if isinstance(a, str) else a
                                for a in (record.args if isinstance(record.args, tuple) else (record.args,)))
        return True


def redact(text: str) -> str:
    return TIN_LIKE.sub("[REDACTED-TIN]", text)
