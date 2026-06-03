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
  file with ``MCPOLIS_INTEGRATION_ENV=/path/to/file``.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_TEST_FILE = Path(
    os.environ.get(
        "MCPOLIS_INTEGRATION_ENV",
        str(Path(__file__).parent / ".env.test"),
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
