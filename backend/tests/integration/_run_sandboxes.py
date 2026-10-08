"""Find and kill the E2B sandboxes that ONE integration test run created.

The paid integration tests share their E2B account with production, so a
run must clean up after itself and touch nothing else. Every sandbox a run
creates carries the run's id in metadata the tests set themselves:

- sandboxes made through ``E2BSandboxService`` carry
  ``mcpolis_instance = "e2e-<...>-<run id>-<...>"``: every test instance
  starts with ``e2e-`` and has the run id as one of its dash-separated
  parts. Production instance ids are database-generated and never start
  with ``e2e-``.
- sandboxes made straight through the client carry
  ``mcpolis_test = "1"`` and ``test_run_id = <run id>``
  (``make_test_metadata`` in the test modules).

A sandbox is this run's only when one of those two holds for this exact
run id; nothing is matched by a broad pattern.

Used by the pytest session hook in ``conftest.py`` (one id per pytest
session, shared with the xdist workers through ``RUN_ID_ENV``), by
``e2b_real_e2e.py`` (its own id), and by ``list_orphan_sandboxes.py
--run-id`` to check that a run left nothing behind.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from mcpolis.adapters.sandbox_e2b import RealE2BClient
from mcpolis.adapters.sandbox_e2b.client import (
    E2BClient,
    E2BNotFoundError,
    E2BSandboxInfo,
    E2BSDKError,
)

RUN_ID_ENV = "MCPOLIS_INTEGRATION_RUN_ID"
TEST_INSTANCE_PREFIX = "e2e-"
# A run id is random hex, at least 8 characters. Anything else ("", "e2e",
# "m6", a typo'd id) would match the fixed parts every test instance
# shares, so it selects nothing.
_RUN_ID_SHAPE = re.compile(r"[0-9a-f]{8,}")


def new_run_id() -> str:
    """A fresh run id: 12 hex characters, no dash."""
    return uuid.uuid4().hex[:12]


def start_run() -> str:
    """Give this pytest session a fresh run id and export it, so the
    xdist workers (spawned later, inheriting the environment) share it.
    Called by the controller only: a value left in the environment by
    an earlier run is replaced, never reused."""
    run_id = new_run_id()
    os.environ[RUN_ID_ENV] = run_id
    return run_id


def current_run_id() -> str:
    """The run id of this pytest session (see :func:`start_run`)."""
    run_id = os.environ.get(RUN_ID_ENV)
    if run_id:
        return run_id
    return start_run()


def is_run_sandbox(metadata: Mapping[str, str], run_id: str) -> bool:
    """True when ``metadata`` marks a sandbox created by run ``run_id``."""
    if _RUN_ID_SHAPE.fullmatch(run_id) is None:
        return False
    instance = metadata.get("mcpolis_instance", "")
    if instance.startswith(TEST_INSTANCE_PREFIX) and run_id in instance.split("-"):
        return True
    return (
        metadata.get("mcpolis_test") == "1"
        and metadata.get("test_run_id") == run_id
    )


def select_run_sandboxes(
    sandboxes: Iterable[E2BSandboxInfo], run_id: str,
) -> list[E2BSandboxInfo]:
    return [s for s in sandboxes if is_run_sandbox(s.metadata, run_id)]


@dataclass
class CleanupReport:
    run_id: str
    killed: list[str] = field(default_factory=lambda: [])
    already_gone: list[str] = field(default_factory=lambda: [])
    failed: list[str] = field(default_factory=lambda: [])

    def lines(self) -> list[str]:
        out = [
            f"E2B cleanup for test run {self.run_id}: "
            f"killed {len(self.killed)}, already gone "
            f"{len(self.already_gone)}, failed {len(self.failed)}",
        ]
        out.extend(f"  killed {sid}" for sid in self.killed)
        out.extend(f"  FAILED {msg}" for msg in self.failed)
        return out


async def kill_run_sandboxes(client: E2BClient, run_id: str) -> CleanupReport:
    """Kill every sandbox (running or paused) that run ``run_id``
    created. A failed list or kill is recorded in the report, never
    raised."""
    report = CleanupReport(run_id=run_id)
    try:
        sandboxes = await client.list_sandboxes()
    except E2BSDKError as exc:
        report.failed.append(f"listing sandboxes: {exc}")
        return report
    for sandbox in select_run_sandboxes(sandboxes, run_id):
        sid = sandbox.sandbox_id
        try:
            await client.kill_sandbox(sid)
            report.killed.append(sid)
        except E2BNotFoundError:
            report.already_gone.append(sid)
        except E2BSDKError as exc:
            report.failed.append(f"{sid}: {exc}")
    return report


def cleanup_run_sandboxes(
    api_key: str | None, run_id: str, log: Callable[[str], None],
) -> None:
    """Blocking entry point for the pytest hook and the standalone
    script: kill run ``run_id``'s sandboxes and ``log`` what happened.
    Never raises an ordinary exception (a cleanup failure must not turn
    a run red or hide its real result)."""
    if not api_key:
        return
    try:
        report = asyncio.run(
            kill_run_sandboxes(RealE2BClient(api_key=api_key), run_id),
        )
    except Exception as exc:  # noqa: BLE001 - logged, never fatal
        log(f"E2B cleanup for test run {run_id} failed: {exc!r}")
        return
    for line in report.lines():
        log(line)


__all__ = [
    "RUN_ID_ENV",
    "CleanupReport",
    "cleanup_run_sandboxes",
    "current_run_id",
    "is_run_sandbox",
    "kill_run_sandboxes",
    "new_run_id",
    "select_run_sandboxes",
    "start_run",
]
