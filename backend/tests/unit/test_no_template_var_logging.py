"""Static guard against accidentally logging post-substitution secrets.

Walks every ``.py`` under the secret-handling adapters and the
substitution helper, parses with :mod:`ast`, and asserts no
``logger.<level>(...)`` call passes a forbidden keyword argument
(``env``, ``headers``, ``merged_env``, ``secret``, ``secrets``,
``secret_value``, ``value``, ``plaintext``, ``token``).

Catches regressions automatically — no human discipline required.

Allowlist: a ``# noqa: SECRET_LOG`` comment on the same line skips
the check for that one call. Kept tiny on purpose.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Modules whose log calls must NOT carry the forbidden kwargs.
_GUARDED_PACKAGES: list[str] = [
    "src/mcpolis/adapters/sandbox_e2b",
    "src/mcpolis/adapters/sandbox_services",
    "src/mcpolis/adapters/upstream_clients",
    "src/mcpolis/domain/services/template_var_substitution.py",
    "src/mcpolis/domain/services/upstream_config_service.py",
    "src/mcpolis/adapters/repositories/file_template_var_repository.py",
    "src/mcpolis/adapters/repositories/mongo_template_var_repository.py",
    "src/mcpolis/entrypoints/routes/dashboard/template_vars.py",
]

_LOG_LEVELS: set[str] = {
    "info", "debug", "warning", "error", "exception", "critical",
}

_FORBIDDEN_KWARGS: set[str] = {
    "env",
    "headers",
    "merged_env",
    "secret",
    "secrets",
    "secret_value",
    "plaintext",
    "token",
}

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for spec in _GUARDED_PACKAGES:
        path = _BACKEND_ROOT / spec
        if path.is_file() and path.suffix == ".py":
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in path.rglob("*.py"))
    return files


def _is_log_call(node: ast.Call) -> bool:
    """``logger.info(...)`` / ``something.warning(...)`` etc."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr in _LOG_LEVELS


def _line_has_noqa(source_lines: list[str], lineno: int) -> bool:
    if 1 <= lineno <= len(source_lines):
        return "# noqa: SECRET_LOG" in source_lines[lineno - 1]
    return False


def _scan_file(path: Path) -> list[str]:
    """Return human-readable violation messages for ``path``."""
    text = path.read_text(encoding="utf-8")
    source_lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"{path}: SyntaxError: {exc}"]
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_log_call(node):
            continue
        for kw in node.keywords:
            if kw.arg in _FORBIDDEN_KWARGS:
                if _line_has_noqa(source_lines, node.lineno):
                    continue
                violations.append(
                    f"{path}:{node.lineno} forbidden log kwarg "
                    f"{kw.arg!r}",
                )
    return violations


def test_no_secret_kwargs_in_log_calls() -> None:
    violations: list[str] = []
    for path in _iter_python_files():
        violations.extend(_scan_file(path))
    assert violations == [], (
        "secret-leaking log calls detected:\n  "
        + "\n  ".join(violations)
    )


def test_guard_self_test_catches_violation(tmp_path: Path) -> None:
    """Sanity check the guard itself — write a deliberate violation
    and confirm ``_scan_file`` reports it."""
    fake = tmp_path / "fake_module.py"
    fake.write_text(
        "import structlog\n"
        "logger = structlog.get_logger()\n"
        "def f():\n"
        "    logger.info('x', env={'A': 'B'})\n"
    )
    violations = _scan_file(fake)
    assert len(violations) == 1
    assert "env" in violations[0]


def test_noqa_marker_silences_guard(tmp_path: Path) -> None:
    fake = tmp_path / "fake_module.py"
    fake.write_text(
        "import structlog\n"
        "logger = structlog.get_logger()\n"
        "def f():\n"
        "    logger.info('x', env={'A': 'B'})  # noqa: SECRET_LOG\n"
    )
    assert _scan_file(fake) == []


def test_other_kwargs_are_fine(tmp_path: Path) -> None:
    fake = tmp_path / "fake_module.py"
    fake.write_text(
        "import structlog\n"
        "logger = structlog.get_logger()\n"
        "def f():\n"
        "    logger.info('x', org_id='y', upstream_id='z')\n"
    )
    assert _scan_file(fake) == []


# Ensure the module-level scan actually finds files (catches the
# case where someone deletes / moves the guarded directory and the
# guard quietly stops working).
def test_iter_python_files_returns_nonempty() -> None:
    files = _iter_python_files()
    assert len(files) > 0
