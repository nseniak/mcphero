"""``acquire_and_refresh_with_recovery``: retry a refresh that hit a
transport stall by reconnecting on a FRESH session.

This is the recovery layer for E2B's intermittent post-reattach stdout
stall — refresh_upstream raises a transport stall, and the wrapper drops
the stalled session (``reconnect_shared_fresh``) and retries so the
operator gets a complete catalogue rather than a partial one.
"""
from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from mcpolis.domain.model.upstream import DiscoveredTool
from mcpolis.domain.services.upstream_connection_service import (
    acquire_and_refresh_with_recovery,
)
from tests.unit.factories import make_upstream_definition


class _FakeManager:
    """Satisfies the slice of ``UpstreamClientManager`` the recovery
    wrapper + ``acquire_upstream_session`` touch for a service_account
    upstream."""

    def __init__(self) -> None:
        self.ensure_calls = 0
        self.fresh_calls = 0

    async def ensure_shared_connected(self, upstream: Any) -> None:
        self.ensure_calls += 1

    def get_session(self, upstream_id: str, user_id: str | None = None) -> Any:
        return object()

    async def reconnect_shared_fresh(self, upstream: Any) -> None:
        self.fresh_calls += 1


class _FakeRegistry:
    """``refresh_upstream`` walks a scripted list of behaviours: an
    exception is raised, anything else is returned (the tool list)."""

    def __init__(self, behaviours: list[Any]) -> None:
        self._behaviours = behaviours
        self.calls = 0

    async def refresh_upstream(self, upstream_id: str) -> list[DiscoveredTool]:
        b = self._behaviours[self.calls]
        self.calls += 1
        if isinstance(b, BaseException):
            raise b
        return cast(list[DiscoveredTool], b)


def make_tool() -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id="everything2",
        original_name="echo",
        prefixed_name="everything2__echo",
        description="echo",
        input_schema={},
    )


async def _run(manager: _FakeManager, registry: _FakeRegistry, **kw: Any):
    upstream = make_upstream_definition(id="everything2")  # service_account
    return await acquire_and_refresh_with_recovery(
        org_id="acme",
        upstream=upstream,
        effective_user="",
        connection_store=None,
        client_manager=cast(Any, manager),
        tool_registry=cast(Any, registry),
        server_url="http://localhost:8000",
        **kw,
    )


@pytest.mark.asyncio
async def test_retries_on_transport_stall_then_succeeds() -> None:
    manager = _FakeManager()
    registry = _FakeRegistry([asyncio.TimeoutError(), [make_tool()]])

    tools = await _run(manager, registry)

    assert len(tools) == 1
    assert registry.calls == 2, "must retry the refresh after a stall"
    assert manager.fresh_calls == 1, "must force a fresh reconnect before retry"


@pytest.mark.asyncio
async def test_non_stall_error_propagates_without_reconnect() -> None:
    manager = _FakeManager()
    registry = _FakeRegistry([RuntimeError("server said no")])

    with pytest.raises(RuntimeError, match="server said no"):
        await _run(manager, registry)

    assert registry.calls == 1, "a non-stall error must not be retried"
    assert manager.fresh_calls == 0, "no fresh reconnect for a non-stall error"


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts() -> None:
    manager = _FakeManager()
    registry = _FakeRegistry([asyncio.TimeoutError(), asyncio.TimeoutError()])

    with pytest.raises(asyncio.TimeoutError):
        await _run(manager, registry, max_attempts=2)

    assert registry.calls == 2, "exactly max_attempts refreshes"
    assert manager.fresh_calls == 1, "one fresh reconnect between the two attempts"
