"""Enforces the dependency rule between layers (imports may only point inward)."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "w9_onboarding"
ROOT_NAME = "w9_onboarding"

# layer -> layers it is allowed to import (besides itself)
ALLOWED = {
    "domain": set(),
    "utils": {"config"},
    "dto": {"domain", "config"},
    "repositories": {"domain", "utils", "config"},
    "infrastructure": {"domain", "utils", "config"},
    "services": {"domain", "dto", "repositories", "infrastructure", "utils", "config"},
    "presentation": {"domain", "dto", "services", "repositories", "infrastructure", "utils", "config"},
}


def _layer(path: Path) -> str:
    rel = path.relative_to(PKG).parts
    return rel[0] if len(rel) > 1 else "config" if path.name == "config.py" else "root"


def _imported_layers(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        mods = []
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append((node.module, [a.name for a in node.names]))
        elif isinstance(node, ast.Import):
            mods += [(a.name, []) for a in node.names]
        for mod, names in mods:
            parts = mod.split(".")
            if parts[0] != ROOT_NAME:
                continue
            if len(parts) == 1:                       # from w9_onboarding import config
                out |= {n for n in names if n == "config"}
            else:
                out.add("config" if parts[1] == "config" else parts[1])
    return out


def test_layers_only_depend_inward():
    violations = []
    for f in PKG.rglob("*.py"):
        layer = _layer(f)
        if layer in ("root", "config") or f.name == "__init__.py":
            continue
        bad = _imported_layers(f) - ALLOWED[layer] - {layer}
        if bad:
            violations.append(f"{f.relative_to(PKG)} ({layer}) imports {sorted(bad)}")
    assert not violations, "\n".join(violations)


def test_every_module_is_in_a_known_layer():
    unknown = {_layer(f) for f in PKG.rglob("*.py") if f.name != "__init__.py"} - set(ALLOWED) - {"config", "root"}
    assert not unknown
