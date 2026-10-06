"""Tenant-scoped access to the supplier master.

The prototype reads `samples/tenants/<tenant_id>/supplier_master.csv` with pandas.
In production this is the Supplier Master API (or a per-tenant Azure AI Search index)
called with a tenant-scoped token - the matcher never sees another tenant's rows because the
repository is *constructed* for exactly one tenant and has no cross-tenant method.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

import pandas as pd
from rapidfuzz import fuzz, process

from w9_onboarding.config import REPO_ROOT
from w9_onboarding.domain.models import SupplierRecord
from w9_onboarding.utils.normalize import normalize_name

DATA_ROOT = REPO_ROOT / "samples" / "tenants"
_TENANT_RE = re.compile(r"^[a-z0-9_\-]{3,64}$")


class TenantNotFound(Exception):
    pass


class SupplierMasterRepository(Protocol):
    tenant_id: str

    def find_by_tin_fingerprint(self, fingerprint: str) -> list[SupplierRecord]: ...
    def search_candidates(self, legal_name: str, dba_name: str | None, zip5: str | None, limit: int = 20) -> list[SupplierRecord]: ...
    def count(self) -> int: ...


class CsvSupplierMaster:
    def __init__(self, tenant_id: str, root: Path = DATA_ROOT) -> None:
        if not _TENANT_RE.match(tenant_id):  # also blocks path traversal ("../other_tenant")
            raise TenantNotFound(f"invalid tenant id '{tenant_id}'")
        path = root / tenant_id / "supplier_master.csv"
        if not path.exists():
            raise TenantNotFound(f"no supplier master for tenant '{tenant_id}'")
        self.tenant_id = tenant_id
        df = pd.read_csv(path, dtype=str).fillna("")
        df = df[df["status"].str.upper() != "INACTIVE"]
        self._records = [SupplierRecord(**{k: (v or None) for k, v in row.items()}) for row in df.to_dict("records")]
        self._by_fp: dict[str, list[SupplierRecord]] = {}
        for r in self._records:
            if r.tin_fingerprint:
                self._by_fp.setdefault(r.tin_fingerprint, []).append(r)
        # Pre-computed normalised name keys = what a search index would hold.
        self._name_keys = [(normalize_name(r.legal_name), normalize_name(r.dba_name)) for r in self._records]

    def count(self) -> int:
        return len(self._records)

    def find_by_tin_fingerprint(self, fingerprint: str) -> list[SupplierRecord]:
        return list(self._by_fp.get(fingerprint, []))

    def search_candidates(self, legal_name: str, dba_name: str | None, zip5: str | None, limit: int = 20) -> list[SupplierRecord]:
        """Blocking / candidate retrieval: cheap recall-oriented filter before precise scoring.

        Union of (a) top-N by token_set_ratio on legal and DBA keys, (b) same-ZIP suppliers.
        """
        queries = [q for q in (normalize_name(legal_name), normalize_name(dba_name)) if q]
        idx: set[int] = set()
        choices = {i: f"{l} | {d}" if d else l for i, (l, d) in enumerate(self._name_keys)}
        for q in queries:
            for _, score, i in process.extract(q, choices, scorer=fuzz.token_set_ratio, limit=limit):
                if score >= 50:
                    idx.add(i)
        if zip5:
            idx |= {i for i, r in enumerate(self._records) if (r.zip or "")[:5] == zip5}
        return [self._records[i] for i in sorted(idx)]
