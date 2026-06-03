"""Contract drift between backend Pydantic response models and frontend
TypeScript interfaces in ``frontend/src/api/types.ts``.

Catches the MCPOLIS-FRONTEND-9 class of bug: a backend response field
gets removed (or renamed) but the frontend's hand-mirrored TS interface
still requires it, so client-side code reads ``undefined.foo`` and
throws at render time. ``tsc`` can't see the mismatch (the TS type just
claims the field exists), and vitest doesn't fault undefined-on-required
without an explicit assertion.

Strategy: AST-parse named backend route modules for ``class Foo(BaseModel)``
declarations and their directly-annotated fields. Regex-parse types.ts
for ``export interface Foo { ... }`` blocks. Match each backend model
to a TS interface by exact name, then by configured prefix fallback,
then diff the two field-name sets.

To extend coverage, append to ``BACKEND_MODULES`` below: one entry per
backend route file, with the list of TS-name prefixes used by the
hand-mirrored interfaces in that area of the frontend.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TYPES_TS = REPO_ROOT / "frontend" / "src" / "api" / "types.ts"

# Each entry: (path to backend route module, list of TS prefixes to try after
# exact name match). Exact match always wins; prefixes are tried in order.
BACKEND_MODULES: list[tuple[str, list[str]]] = [
    ("backend/src/mcpolis/entrypoints/routes/superadmin_routes.py", ["Superadmin"]),
]


def collect_backend_models(file_path: Path) -> dict[str, set[str]]:
    """Return ``{ClassName: {field_name, ...}}`` for every Pydantic model
    declared in ``file_path``.

    A class is treated as a Pydantic model if any base name (or attribute
    suffix) is ``BaseModel``. Only directly-annotated fields are returned —
    inherited fields are out of scope for v1 because the route-module
    response models in this repo don't currently chain.
    """
    tree = ast.parse(file_path.read_text(), filename=str(file_path))
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_pydantic = False
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "BaseModel":
                is_pydantic = True
                break
            if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
                is_pydantic = True
                break
        if not is_pydantic:
            continue
        fields: set[str] = set()
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.add(item.target.id)
        out[node.name] = fields
    return out


_INTERFACE_RE = re.compile(r"^export interface (\w+)\b[^{]*\{")
_FIELD_RE = re.compile(r"^\s*(\w+)\??\s*:")


def parse_ts_interfaces(ts_source: str) -> dict[str, set[str]]:
    """Return ``{InterfaceName: {field_name, ...}}`` from a types.ts source.

    Tracks brace depth so that nested object-literal types inside fields
    don't pollute the parent interface's field set. Skips comments and
    union/literal noise — only the leading ``name:`` or ``name?:`` token
    on a line is captured.
    """
    interfaces: dict[str, set[str]] = {}
    lines = ts_source.splitlines()
    i = 0
    while i < len(lines):
        m = _INTERFACE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        fields: set[str] = set()
        depth = lines[i].count("{") - lines[i].count("}")
        i += 1
        while i < len(lines) and depth > 0:
            line = lines[i]
            if depth == 1:
                fm = _FIELD_RE.match(line)
                if fm:
                    fields.add(fm.group(1))
            depth += line.count("{") - line.count("}")
            i += 1
        interfaces[name] = fields
    return interfaces


def _resolve_ts_name(
    py_name: str, prefixes: list[str], ts_interfaces: dict[str, set[str]]
) -> str | None:
    if py_name in ts_interfaces:
        return py_name
    for prefix in prefixes:
        candidate = f"{prefix}{py_name}"
        if candidate in ts_interfaces:
            return candidate
    return None


def find_contract_drift(
    backend_modules: list[tuple[str, list[str]]],
    types_ts_path: Path,
) -> list[str]:
    """Return one human-readable drift line per mismatched pair. Empty
    list means clean. Backend models with no matching TS interface are
    skipped silently (the frontend may legitimately not mirror them).
    """
    ts_interfaces = parse_ts_interfaces(types_ts_path.read_text())
    drifts: list[str] = []
    for rel_path, prefixes in backend_modules:
        py_file = REPO_ROOT / rel_path
        models = collect_backend_models(py_file)
        for py_name, py_fields in models.items():
            ts_name = _resolve_ts_name(py_name, prefixes, ts_interfaces)
            if ts_name is None:
                continue
            ts_fields = ts_interfaces[ts_name]
            extra_in_ts = ts_fields - py_fields
            extra_in_py = py_fields - ts_fields
            if not (extra_in_ts or extra_in_py):
                continue
            parts = [f"{py_name} ({rel_path}) <-> {ts_name}"]
            if extra_in_ts:
                parts.append(
                    f"frontend declares fields the backend won't send "
                    f"(undefined at runtime): {sorted(extra_in_ts)}"
                )
            if extra_in_py:
                parts.append(
                    f"backend sends fields the frontend type doesn't declare: "
                    f"{sorted(extra_in_py)}"
                )
            drifts.append(" | ".join(parts))
    return drifts


def test_no_contract_drift_on_listed_route_modules() -> None:
    drifts = find_contract_drift(BACKEND_MODULES, TYPES_TS)
    assert not drifts, "Frontend/backend contract drift:\n  " + "\n  ".join(drifts)


def test_ts_interface_parser_extracts_fields() -> None:
    sample = """
export interface SimpleThing {
  foo: string;
  bar?: number;
  baz: { nested: string; another: number };
  qux: "a" | "b";
}

export interface OtherThing {
  alpha: boolean;
}
"""
    parsed = parse_ts_interfaces(sample)
    assert parsed == {
        "SimpleThing": {"foo", "bar", "baz", "qux"},
        "OtherThing": {"alpha"},
    }


def test_pydantic_field_extraction_via_ast(tmp_path: Path) -> None:
    src = '''
from pydantic import BaseModel
from typing import Literal


class NotPydantic:
    field_we_should_skip: int = 0


class Inner(BaseModel):
    a: str
    b: int


class Outer(BaseModel):
    mode: Literal["x", "y"]
    nested: Inner
    flag: bool
'''
    f = tmp_path / "fake_routes.py"
    f.write_text(src)
    out = collect_backend_models(f)
    assert out == {
        "Inner": {"a", "b"},
        "Outer": {"mode", "nested", "flag"},
    }


def test_drift_detection_flags_extra_frontend_field(tmp_path: Path) -> None:
    """Synthetic round-trip: a fake backend module that drops a field the
    fake TS interface still requires must surface as drift."""
    py = tmp_path / "fake_routes.py"
    py.write_text(
        "from pydantic import BaseModel\n"
        "class Thing(BaseModel):\n"
        "    a: str\n"
        "    b: int\n"
    )
    ts = tmp_path / "types.ts"
    ts.write_text(
        "export interface Thing {\n"
        "  a: string;\n"
        "  b: number;\n"
        "  c: string;\n"
        "}\n"
    )
    py_fields = collect_backend_models(py)["Thing"]
    ts_fields = parse_ts_interfaces(ts.read_text())["Thing"]
    assert ts_fields - py_fields == {"c"}
    assert py_fields - ts_fields == set()
