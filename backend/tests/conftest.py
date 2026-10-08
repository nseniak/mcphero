"""Hooks shared by every backend pytest suite (unit, integration)."""
from __future__ import annotations

import os

import pytest


def pytest_unconfigure(config: pytest.Config) -> None:
    """Repeat the run's results folder as the last line of the output.

    The runner scripts (backend/run-unit-tests.sh, run-integration-tests.sh,
    tests/integration/run-e2b-broad-matrix.sh) print the folder at the start
    and export it as MCPOLIS_TEST_OUT_DIR. They ``exec`` pytest, so that a
    signal sent to the runner reaches pytest itself; only pytest is left to
    print the folder last. ``pytest_unconfigure`` runs after the final
    "N passed" line. See tests/run_folder.py at the repo root."""
    folder = os.environ.get("MCPOLIS_TEST_OUT_DIR")
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    is_xdist_worker = hasattr(config, "workerinput")
    if folder and reporter is not None and not is_xdist_worker:
        reporter.write_line(f"Results folder: {folder}")
