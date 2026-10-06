"""TIN vault (tokenisation store). Prototype stand-in: in-memory, placeholder crypto marking the seam
where Key Vault / HSM envelope encryption goes in production."""
from __future__ import annotations

import uuid

from w9_onboarding.utils.security import tenant_key

class TinVault:
    """Stand-in for the tokenisation / vault service.

    The prototype keeps ciphertext in memory only - it exists so the response contract
    can carry an opaque `vault_ref` exactly as production would.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, tenant_id: str, tin_digits: str) -> str:
        ref = f"vault://{tenant_id}/tin/{uuid.uuid4().hex[:16]}"
        # Placeholder "encryption" (XOR with tenant key stream) - NOT real crypto, marks the seam.
        key = tenant_key(tenant_id)
        self._store[ref] = bytes(b ^ key[i % len(key)] for i, b in enumerate(tin_digits.encode()))
        return ref

    def reveal(self, tenant_id: str, ref: str) -> str:
        """Detokenise. In production: separate service, separate entitlement, every call audited."""
        if not ref.startswith(f"vault://{tenant_id}/"):
            raise PermissionError("vault reference belongs to a different tenant")
        key = tenant_key(tenant_id)
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(self._store[ref])).decode()
