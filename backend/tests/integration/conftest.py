"""Environment bootstrap for the real-SDK E2B integration suite.

Loads ``backend/tests/integration/.env.test`` (gitignored) so the tests
find their E2B API key the same way no matter how pytest is launched:
through ``run-integration-tests.sh`` or a bare ``pytest
tests/integration/...``. See ``.env.test.example`` for the variables.

This runs at conftest *import* time (top-level, not in a fixture) on
purpose: pytest imports conftest before the test modules, and each test
module reads the key at module load to build its ``skipif`` marker, so a
fixture would run too late.

Loading rules:
- Variables already present in the environment win over the file, so CI
  can inject secrets without a file on disk.
- ``MCPOLIS_E2B_API_KEY`` is the canonical name (the app's Settings read
  it). The E2B SDK and the test code read the bare ``E2B_API_KEY``, so we
  mirror the canonical value into it. Set only ``MCPOLIS_E2B_API_KEY``.
- We read ``.env.test`` only, never prod secrets. Point at a different
  file with ``MCPOLIS_INTEGRATION_ENV=/path/to/file``. In a git worktree
  without its own copy, the main checkout's is used (``_env_file.py``).
- No key after loading prints one warning: every paid test then skips,
  and a skip-only run would otherwise read as a pass.

The tests share their E2B account with production, so each pytest session
deletes the sandboxes it created, and only those, when it ends (passed,
failed or interrupted): see ``pytest_sessionfinish`` and
``_run_sandboxes.py``. The session's run id is chosen here, at import
time, by the controller, and reaches the xdist workers through the
environment they inherit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._env_file import default_env_file
from tests.integration._run_sandboxes import (
    cleanup_run_sandboxes,
    current_run_id,
    start_run,
)

_INTEGRATION_DIR = Path(__file__).resolve().parent
_ENV_TEST_FILE = Path(
    os.environ.get(
        "MCPOLIS_INTEGRATION_ENV",
        str(default_env_file(_INTEGRATION_DIR, _INTEGRATION_DIR.parents[2])),
    )
)


def _load_env_file(path: Path) -> None:
    """Export ``KEY=VALUE`` lines from ``path``; already-set env wins."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def _mirror_e2b_key() -> None:
    """Keep MCPOLIS_E2B_API_KEY and E2B_API_KEY in sync (canonical first)."""
    canonical = os.environ.get("MCPOLIS_E2B_API_KEY")
    bare = os.environ.get("E2B_API_KEY")
    if canonical and not bare:
        os.environ["E2B_API_KEY"] = canonical
    elif bare and not canonical:
        os.environ["MCPOLIS_E2B_API_KEY"] = bare


_load_env_file(_ENV_TEST_FILE)
_mirror_e2b_key()
_NO_KEY_WARNING = (
    None
    if os.environ.get("E2B_API_KEY")
    else f"WARNING: no E2B key (looked in {_ENV_TEST_FILE}): "
    "every paid integration test SKIPS"
)


def pytest_report_header() -> str | None:
    return _NO_KEY_WARNING


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if _NO_KEY_WARNING is not None:
        terminalreporter.write_line(_NO_KEY_WARNING, red=True, bold=True)

# An xdist worker inherits the controller's id; anything else starts a run.
if os.environ.get("PYTEST_XDIST_WORKER") is None:
    start_run()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Kill every E2B sandbox this session's tests created.

    Runs once, in the controller (or the only process without xdist),
    after every test ended, also on failure and Ctrl-C. A failed kill is
    reported, never fatal."""
    del exitstatus
    if hasattr(session.config, "workerinput"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.ensure_newline()

    def log(line: str) -> None:
        if reporter is not None:
            reporter.write_line(line)
        else:
            print(line)

    cleanup_run_sandboxes(
        os.environ.get("E2B_API_KEY"), current_run_id(), log,
    )
