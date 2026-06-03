"""Tests for the upstream MCP client message handler and its plumbing."""
from __future__ import annotations

import pytest
import mcp.types as mcp_types

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.adapters.upstream_clients.notification_handler import (
    build_tool_change_message_handler,
)
from tests.unit.factories import make_upstream_definition


def _make_tool_list_changed_notification() -> mcp_types.ServerNotification:
    return mcp_types.ServerNotification(mcp_types.ToolListChangedNotification())


def _make_other_server_notification() -> mcp_types.ServerNotification:
    return mcp_types.ServerNotification(
        mcp_types.ResourceListChangedNotification(),
    )


@pytest.mark.asyncio
async def test_handler_fires_on_tool_list_changed() -> None:
    calls: list[int] = []
    handler = build_tool_change_message_handler(
        "upstream-1", lambda: calls.append(1),
    )
    await handler(_make_tool_list_changed_notification())
    assert calls == [1]


@pytest.mark.asyncio
async def test_handler_ignores_other_notifications() -> None:
    calls: list[int] = []
    handler = build_tool_change_message_handler(
        "upstream-1", lambda: calls.append(1),
    )
    await handler(_make_other_server_notification())
    assert calls == []


@pytest.mark.asyncio
async def test_handler_ignores_exceptions_and_other_inputs() -> None:
    calls: list[int] = []
    handler = build_tool_change_message_handler(
        "upstream-1", lambda: calls.append(1),
    )
    await handler(RuntimeError("something"))
    # No callback set — tool-list-changed also a no-op
    silent = build_tool_change_message_handler("upstream-1", None)
    await silent(_make_tool_list_changed_notification())
    assert calls == []


@pytest.mark.asyncio
async def test_handler_swallows_callback_exceptions() -> None:
    def boom() -> None:
        raise RuntimeError("callback failed")

    handler = build_tool_change_message_handler("upstream-1", boom)
    # Must not propagate — the ClientSession task must keep running.
    await handler(_make_tool_list_changed_notification())


def test_client_manager_wraps_callback_with_upstream_id() -> None:
    received: list[str] = []
    mgr = UpstreamClientManager([make_upstream_definition(id="gh")])
    mgr.set_on_upstream_tools_changed(lambda uid: received.append(uid))

    cb = mgr._build_tool_change_cb("gh")  # pyright: ignore[reportPrivateUsage]
    assert cb is not None
    cb()
    assert received == ["gh"]


def test_client_manager_build_cb_returns_none_when_unset() -> None:
    mgr = UpstreamClientManager([make_upstream_definition(id="gh")])
    assert mgr._build_tool_change_cb("gh") is None  # pyright: ignore[reportPrivateUsage]
