"""The backend's own time bounds fit the container's, in
docker-compose.yml.

- A deploy's ``docker stop`` sends SIGTERM, then kills the backend once
  ``stop_grace_period`` is over. The request drain, uvicorn's graceful
  shutdown and every step of the shutdown cleanup (``ShutdownBudget``)
  must fit in it with time to spare, or a slow shutdown is killed before
  the stores close (sign-in revokes lost, audit rows lost).
- Nothing answers ``/health`` until the boot is done, and the health
  check marks the backend unhealthy after a few failures past its start
  period; a deploy then never starts nginx (``depends_on:
  service_healthy``). The boot's E2B cleanup, the one boot step that
  waits on a third party, must stay well under that.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from mcpolis.adapters.upstream_clients.connection_task_base import (
    ABANDON_TIMEOUT,
)
from mcpolis.domain.services.tool_router import AUDIT_WRITE_TIMEOUT_SECONDS
from mcpolis.entrypoints.app import BOOT_RECONCILE_TIMEOUT_SECONDS
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.admin_tool_calls import (
    WAIT_AFTER_CANCEL_SECONDS,
)
from mcpolis.entrypoints.lifecycle import ShutdownBudget

_COMPOSE_FILE = Path(__file__).resolve().parents[3] / "docker-compose.yml"
# uvicorn's main loop notices the exit request on its next 0.1 s tick,
# then sleeps 0.1 s before it waits for open connections.
_UVICORN_SHUTDOWN_TICKS_SECONDS = 0.2
# What the grace must leave once every bound above is spent.
_SPARE_SECONDS = 10.0


def make_compose_services() -> dict[str, Any]:
    compose: dict[str, Any] = yaml.safe_load(_COMPOSE_FILE.read_text())
    return compose["services"]


def make_seconds(duration: str) -> float:
    """A compose duration (``90s``, ``1m30s``) in seconds."""
    parts = re.fullmatch(r"(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", duration)
    assert parts is not None and duration, duration
    minutes, seconds = parts.groups()
    return int(minutes or 0) * 60 + float(seconds or 0)


def make_default_setting(name: str) -> float:
    default = Settings.model_fields[name].default
    assert isinstance(default, int | float), (name, default)
    return float(default)


def test_the_worst_case_shutdown_fits_the_stop_grace_period() -> None:
    before_cleanup = (
        make_default_setting("drain_timeout")
        + _UVICORN_SHUTDOWN_TICKS_SECONDS
        + make_default_setting("graceful_shutdown_timeout")
    )
    worst_case = before_cleanup + ShutdownBudget().worst_case_seconds()

    for service in ("backend", "standalone"):
        grace = make_seconds(make_compose_services()[service]["stop_grace_period"])
        assert worst_case + _SPARE_SECONDS <= grace, (
            f"{service}: the shutdown can take {worst_case:.1f} s, which "
            f"leaves less than {_SPARE_SECONDS:.0f} s of its {grace:.0f} s "
            "stop_grace_period: Docker could kill it before the stores close"
        )


def test_the_shutdown_waits_for_a_cancelled_mcp_call_as_long_as_it_waits() -> None:
    """Closing the MCP sessions cancels every call in flight: a gateway
    tool call then writes its audit row (``AUDIT_WRITE_TIMEOUT_SECONDS``),
    an Admin MCP or operator MCP call waits for its action
    (``WAIT_AFTER_CANCEL_SECONDS``). The step must wait longer than both,
    or every such call makes it log ``app.shutdown.step_timed_out`` (the
    step gave 6 s to the Admin MCP's 10 s wait)."""
    budget = ShutdownBudget().mcp_sessions

    assert budget > WAIT_AFTER_CANCEL_SECONDS, (budget, WAIT_AFTER_CANCEL_SECONDS)
    assert budget > AUDIT_WRITE_TIMEOUT_SECONDS, (budget, AUDIT_WRITE_TIMEOUT_SECONDS)


def test_the_shutdown_waits_for_a_stopped_connect_as_long_as_it_waits() -> None:
    """A connect the shutdown aborts may wait ``ABANDON_TIMEOUT`` for the
    sandbox it was creating, then records the sandbox it keeps. The job
    drain must wait that long too, or the stores close first and the next
    boot cannot find that sandbox (it kills it as an orphan and creates
    another)."""
    assert ShutdownBudget().background_jobs >= ABANDON_TIMEOUT


def test_the_boot_cleanup_ends_well_before_the_health_check_gives_up() -> None:
    check = make_compose_services()["backend"]["healthcheck"]
    # The earliest the backend can be marked unhealthy: the failures that
    # count start after the start period, one per interval.
    unhealthy_after = (
        make_seconds(check["start_period"])
        + (int(check["retries"]) - 1) * make_seconds(check["interval"])
    )

    assert BOOT_RECONCILE_TIMEOUT_SECONDS <= unhealthy_after / 2, (
        f"a hung E2B listing holds the boot {BOOT_RECONCILE_TIMEOUT_SECONDS:.0f} s "
        f"while the health check can mark the backend unhealthy after "
        f"{unhealthy_after:.0f} s"
    )
