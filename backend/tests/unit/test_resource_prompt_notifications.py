"""End-to-end forwarding of upstream ``resources/list_changed`` and
``prompts/list_changed`` notifications.

Mirrors ``test_policy_notifier.py``'s upstream-tools-changed style: drive
the notifier with a registered session, fire the relevant notification,
and assert the matching JSONRPCNotification reaches the session writer.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest

from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.adapters.upstream_clients.notification_handler import (
    build_tool_change_message_handler,
)
from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
from mcpolis.domain.services.policy_notifier import PolicyNotifier
from tests.unit.factories import make_upstream_definition


def _make_session_manager_with_writer() -> tuple[Any, MagicMock]:
    """Build a stub StreamableHTTPSessionManager whose write stream
    captures every ``send_nowait`` call. Returns ``(manager, writer)``."""
    writer = MagicMock()
    writer.send_nowait = MagicMock()
    transport = MagicMock()
    transport._write_stream = writer
    manager = MagicMock()
    manager._server_instances = {"sid-1": transport}
    return manager, writer


def _make_registry_with_session(org_id: str) -> GatewaySessionRegistry:
    reg = GatewaySessionRegistry()
    reg.register("sid-1", user_id="alice", org_id=org_id)
    return reg


def _make_runtime_manager_with_runtime(
    org_id: str, *, refresh_resources_called: list[str],
    refresh_prompts_called: list[str],
) -> OrgRuntimeManager:
    upstream = make_upstream_definition(id="notion")
    cm = UpstreamClientManager([upstream])

    tool_registry = MagicMock()
    tool_registry.refresh_resources_for_upstream = AsyncMock(
        side_effect=lambda uid: refresh_resources_called.append(uid)
    )
    tool_registry.refresh_prompts_for_upstream = AsyncMock(
        side_effect=lambda uid: refresh_prompts_called.append(uid)
    )

    runtime = OrgRuntime(
        org_id=org_id,
        policy_engine=MagicMock(),
        tool_registry=tool_registry,
        client_manager=cm,
        tool_router=MagicMock(),
        config_service=MagicMock(),
        upstreams=[upstream],
    )

    manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )
    manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]
    manager._startup_status[org_id] = MagicMock(  # pyright: ignore[reportPrivateUsage]
        ready=True, total=0, connected=set(), failed=set(),
    )
    return manager


# ─── notification_handler ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_fires_on_resource_list_changed() -> None:
    calls: list[int] = []
    handler = build_tool_change_message_handler(
        "upstream-1",
        on_tool_list_changed=None,
        on_resource_list_changed=lambda: calls.append(1),
    )
    notif = mcp_types.ServerNotification(
        mcp_types.ResourceListChangedNotification(),
    )
    await handler(notif)
    assert calls == [1]


@pytest.mark.asyncio
async def test_handler_fires_on_prompt_list_changed() -> None:
    calls: list[int] = []
    handler = build_tool_change_message_handler(
        "upstream-1",
        on_tool_list_changed=None,
        on_prompt_list_changed=lambda: calls.append(1),
    )
    notif = mcp_types.ServerNotification(
        mcp_types.PromptListChangedNotification(),
    )
    await handler(notif)
    assert calls == [1]


@pytest.mark.asyncio
async def test_handler_does_not_cross_wire_callbacks() -> None:
    """A resource notification must not fire the prompt callback (and
    vice versa) — regression test for a future bug where the union dispatch
    drops the isinstance check."""
    tool_calls: list[int] = []
    resource_calls: list[int] = []
    prompt_calls: list[int] = []
    handler = build_tool_change_message_handler(
        "upstream-1",
        on_tool_list_changed=lambda: tool_calls.append(1),
        on_resource_list_changed=lambda: resource_calls.append(1),
        on_prompt_list_changed=lambda: prompt_calls.append(1),
    )
    await handler(
        mcp_types.ServerNotification(
            mcp_types.ResourceListChangedNotification(),
        )
    )
    assert tool_calls == []
    assert resource_calls == [1]
    assert prompt_calls == []


def test_client_manager_wires_resource_callback_with_upstream_id() -> None:
    received: list[str] = []
    mgr = UpstreamClientManager([make_upstream_definition(id="notion")])
    mgr.set_on_upstream_resources_changed(lambda uid: received.append(uid))

    cb = mgr._build_resource_change_cb("notion")  # pyright: ignore[reportPrivateUsage]
    assert cb is not None
    cb()
    assert received == ["notion"]


def test_client_manager_wires_prompt_callback_with_upstream_id() -> None:
    received: list[str] = []
    mgr = UpstreamClientManager([make_upstream_definition(id="notion")])
    mgr.set_on_upstream_prompts_changed(lambda uid: received.append(uid))

    cb = mgr._build_prompt_change_cb("notion")  # pyright: ignore[reportPrivateUsage]
    assert cb is not None
    cb()
    assert received == ["notion"]


# ─── PolicyNotifier — debounced refresh + broadcast ───────────────────────


@pytest.mark.asyncio
async def test_notify_upstream_resources_refreshes_and_broadcasts() -> None:
    org_id = "acme-id"
    refresh_resources: list[str] = []
    refresh_prompts: list[str] = []

    rm = _make_runtime_manager_with_runtime(
        org_id,
        refresh_resources_called=refresh_resources,
        refresh_prompts_called=refresh_prompts,
    )
    sm, writer = _make_session_manager_with_writer()
    reg = _make_registry_with_session(org_id)
    notifier = PolicyNotifier(
        sm, reg, rm, debounce_seconds=0.0,
    )

    notifier.notify_upstream_resources_changed(org_id, "notion")

    # Debounce 0.0s + asyncio.create_task → one event-loop spin.
    for _ in range(20):
        if writer.send_nowait.call_count > 0:
            break
        await asyncio.sleep(0.01)

    assert refresh_resources == ["notion"]
    assert writer.send_nowait.call_count == 1
    sent_msg = writer.send_nowait.call_args.args[0]
    assert (
        sent_msg.message.root.method
        == "notifications/resources/list_changed"
    )


@pytest.mark.asyncio
async def test_notify_upstream_prompts_refreshes_and_broadcasts() -> None:
    org_id = "acme-id"
    refresh_resources: list[str] = []
    refresh_prompts: list[str] = []

    rm = _make_runtime_manager_with_runtime(
        org_id,
        refresh_resources_called=refresh_resources,
        refresh_prompts_called=refresh_prompts,
    )
    sm, writer = _make_session_manager_with_writer()
    reg = _make_registry_with_session(org_id)
    notifier = PolicyNotifier(
        sm, reg, rm, debounce_seconds=0.0,
    )

    notifier.notify_upstream_prompts_changed(org_id, "notion")

    for _ in range(20):
        if writer.send_nowait.call_count > 0:
            break
        await asyncio.sleep(0.01)

    assert refresh_prompts == ["notion"]
    assert writer.send_nowait.call_count == 1
    sent_msg = writer.send_nowait.call_args.args[0]
    assert (
        sent_msg.message.root.method
        == "notifications/prompts/list_changed"
    )


@pytest.mark.asyncio
async def test_existing_tools_changed_path_still_works() -> None:
    """Regression guard: adding the resource / prompt code paths must
    not break the existing tool-list-changed broadcast."""
    org_id = "acme-id"
    upstream = make_upstream_definition(id="notion")
    cm = UpstreamClientManager([upstream])
    tool_registry = MagicMock()
    tool_registry.refresh_upstream = AsyncMock(return_value=[])

    runtime = OrgRuntime(
        org_id=org_id,
        policy_engine=MagicMock(),
        tool_registry=tool_registry,
        client_manager=cm,
        tool_router=MagicMock(),
        config_service=MagicMock(),
        upstreams=[upstream],
    )

    manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )
    manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]
    manager._startup_status[org_id] = MagicMock(  # pyright: ignore[reportPrivateUsage]
        ready=True, total=0, connected=set(), failed=set(),
    )

    sm, writer = _make_session_manager_with_writer()
    reg = _make_registry_with_session(org_id)
    notifier = PolicyNotifier(sm, reg, manager, debounce_seconds=0.0)

    notifier.notify_upstream_tools_changed(org_id, "notion")
    for _ in range(20):
        if writer.send_nowait.call_count > 0:
            break
        await asyncio.sleep(0.01)

    assert writer.send_nowait.call_count == 1
    sent_msg = writer.send_nowait.call_args.args[0]
    assert (
        sent_msg.message.root.method
        == "notifications/tools/list_changed"
    )
