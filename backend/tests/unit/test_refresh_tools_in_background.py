"""Unit tests for ``refresh_tools_in_background``.

The dashboard refresh endpoint is non-blocking: it kicks the acquire+
refresh off in a background task so an E2B-pause stall can't blow the
request budget (the 2026-06-18 incident, where the SYNCHRONOUS refresh
surfaced a TimeoutError for a refresh that succeeded in the background).
These tests pin that helper directly — that the refreshing flag flips
synchronously, the recovery runs in the task, and on_success/on_error
fire with the right message — by awaiting the returned task.
``acquire_and_refresh_with_recovery`` is mocked at the module boundary;
the refresh-display floor is zeroed so the task doesn't sleep.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    _refresh_upstream_in_background,  # pyright: ignore[reportPrivateUsage]
    SessionUnavailable,
    refresh_tools_in_background,
)
from mcpolis.domain.services.secret_scanner import HIDDEN_VALUE
from tests.unit.factories import make_upstream_definition
from tests.unit.test_connect_errors_hide_passwords import (
    PASSWORD,
    make_http_upstream,
    make_status_error_text,
    make_vars,
)

_ACQUIRE = (
    "mcpolis.domain.services.upstream_connection_service"
    ".acquire_and_refresh_with_recovery"
)
_MIN = (
    "mcpolis.domain.services.upstream_connection_service"
    "._MIN_REFRESHING_DISPLAY_SECONDS"
)


class _FakeToolRegistry:
    """Minimal stand-in exposing only the refreshing-flag surface the
    helper touches (acquire_and_refresh_with_recovery is mocked, so the
    registry is never used for a real refresh)."""

    def __init__(self) -> None:
        self.marked: list[str] = []
        self.unmarked: list[str] = []
        self._started: dict[str, float] = {}

    def mark_refreshing(self, upstream_id: str) -> None:
        self.marked.append(upstream_id)
        self._started[upstream_id] = time.monotonic()

    def unmark_refreshing(self, upstream_id: str) -> None:
        self.unmarked.append(upstream_id)

    def refreshing_started_at(self, upstream_id: str) -> float | None:
        return self._started.get(upstream_id)


def _make_callbacks() -> tuple[
    dict[str, object],
    Callable[[], Awaitable[None]],
    Callable[[str], Awaitable[None]],
]:
    seen: dict[str, object] = {}

    async def on_success() -> None:
        seen["success"] = True

    async def on_error(msg: str) -> None:
        seen["error"] = msg

    return seen, on_success, on_error


def _kick(
    reg: _FakeToolRegistry,
    on_success: Callable[[], Awaitable[None]],
    on_error: Callable[[str], Awaitable[None]],
    client_manager: UpstreamClientManager | None = None,
    upstream: UpstreamDefinition | None = None,
) -> "asyncio.Task[None]":
    return refresh_tools_in_background(
        org_id="o1",
        upstream=upstream or make_upstream_definition(id="u1", command="npx"),
        effective_user="",
        connection_store=None,
        client_manager=client_manager or UpstreamClientManager([]),
        tool_registry=cast(ToolRegistry, reg),
        server_url="http://localhost:8000",
        on_success=on_success,
        on_error=on_error,
    )


async def test_marks_refreshing_synchronously_then_clears_on_success() -> None:
    reg = _FakeToolRegistry()
    seen, on_success, on_error = _make_callbacks()
    with patch(_ACQUIRE, new_callable=AsyncMock), patch(_MIN, 0.0):
        task = _kick(reg, on_success, on_error)
        # The flag flips BEFORE the task body runs, so the very next
        # GET /upstreams shows the "Fetching info" pill.
        assert reg.marked == ["u1"]
        await task
    assert seen.get("success") is True
    assert "error" not in seen
    assert reg.unmarked == ["u1"]


async def test_discovery_failure_calls_on_error_and_clears_flag() -> None:
    reg = _FakeToolRegistry()
    seen, on_success, on_error = _make_callbacks()
    with patch(
        _ACQUIRE, new_callable=AsyncMock,
        side_effect=RuntimeError("list_tools blew up"),
    ), patch(_MIN, 0.0):
        await _kick(reg, on_success, on_error)
    assert seen.get("error") == "list_tools blew up"
    assert "success" not in seen
    # The pill is always cleared, even on failure.
    assert reg.unmarked == ["u1"]


async def test_session_unavailable_is_formatted_for_the_banner() -> None:
    reg = _FakeToolRegistry()
    seen, on_success, on_error = _make_callbacks()
    with patch(
        _ACQUIRE, new_callable=AsyncMock,
        side_effect=SessionUnavailable("connect_failed"),
    ), patch(_MIN, 0.0):
        await _kick(reg, on_success, on_error)
    assert seen.get("error") == "could not reattach session: connect_failed"
    assert reg.unmarked == ["u1"]


def make_refreshing_registry(refresh: AsyncMock) -> MagicMock:
    registry = MagicMock()
    registry.refreshing_started_at.return_value = None
    registry.refresh_upstream = refresh
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refresh", "outcome"),
    [
        (AsyncMock(return_value=[]), None),
        (AsyncMock(side_effect=RuntimeError("list_tools timed out")),
         "list_tools timed out"),
        (AsyncMock(side_effect=asyncio.CancelledError()),
         "tool discovery was cancelled"),
    ],
)
async def test_background_refresh_reports_the_discovery_outcome(
    refresh: AsyncMock, outcome: str | None,
) -> None:
    """``on_discovery_done`` hears every outcome, a cancellation
    included, so an Admin MCP sign-in never waits on a discovery that
    ended."""
    outcomes: list[str | None] = []

    try:
        await _refresh_upstream_in_background(
            make_refreshing_registry(refresh), UpstreamClientManager([]), "u",
            on_discovery_done=outcomes.append,
        )
    except asyncio.CancelledError:
        pass

    assert outcomes == [outcome]


async def make_manager_holding_password(
    tmp_path: Path,
) -> tuple[UpstreamClientManager, UpstreamDefinition, str]:
    """A manager that substituted ``PASSWORD`` into ``web``'s URL, and
    the error httpx gives when that URL is refused."""
    upstream = make_http_upstream("https://mcp.example.com/mcp?q=${PW}")
    manager = UpstreamClientManager(
        [upstream], template_var_repo=await make_vars(tmp_path, "web"),
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # pyright: ignore[reportPrivateUsage]
    assert resolved.http is not None
    return manager, upstream, make_status_error_text(resolved.http.url)


async def test_refresh_tools_error_carries_no_password(tmp_path: Path) -> None:
    """Saved, audited and answered to the admin who clicked Refresh."""
    manager, upstream, error = await make_manager_holding_password(tmp_path)
    reg = _FakeToolRegistry()
    seen, on_success, on_error = _make_callbacks()
    with patch(
        _ACQUIRE, new_callable=AsyncMock, side_effect=RuntimeError(error),
    ), patch(_MIN, 0.0):
        await _kick(reg, on_success, on_error, manager, upstream)
    shown = cast(str, seen.get("error"))
    assert "401 Unauthorized" in shown
    assert HIDDEN_VALUE in shown
    assert PASSWORD.split()[0] not in shown


async def test_discovery_after_connect_reports_no_password(
    tmp_path: Path,
) -> None:
    manager, upstream, error = await make_manager_holding_password(tmp_path)
    outcomes: list[str | None] = []
    await _refresh_upstream_in_background(
        make_refreshing_registry(AsyncMock(side_effect=RuntimeError(error))),
        manager, upstream.id,
        on_discovery_done=outcomes.append,
    )
    [shown] = outcomes
    assert shown is not None and HIDDEN_VALUE in shown
    assert PASSWORD.split()[0] not in shown
