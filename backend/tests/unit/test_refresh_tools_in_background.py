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
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    SessionUnavailable,
    refresh_tools_in_background,
)
from tests.unit.factories import make_upstream_definition

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
) -> "asyncio.Task[None]":
    return refresh_tools_in_background(
        org_id="o1",
        upstream=make_upstream_definition(id="u1", command="npx"),
        effective_user="",
        connection_store=None,
        client_manager=cast(UpstreamClientManager, MagicMock()),
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
