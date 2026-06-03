from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.policy_notifier import PolicyNotifier
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit.factories import make_runtime_manager, make_upstream_definition


def make_config(
    users: dict[str, UserDefinition] | None = None,
    roles: dict[str, RoleDefinition] | None = None,
) -> SettingsConfig:
    return SettingsConfig(
        users=users or {},
        roles=roles or {"viewer": RoleDefinition(settings=RoleSettings())},
    )


def make_mock_transport() -> MagicMock:
    transport = MagicMock()
    transport._write_stream = MagicMock()
    transport._write_stream.send_nowait = MagicMock()
    return transport


def make_notifier(
    users: dict[str, UserDefinition] | None = None,
    roles: dict[str, RoleDefinition] | None = None,
    debounce_seconds: float = 0.05,
) -> tuple[PolicyNotifier, GatewaySessionRegistry, MagicMock]:
    config = make_config(users=users, roles=roles)
    policy_engine = PolicyEngine(config)
    registry = GatewaySessionRegistry()
    session_manager = MagicMock()
    session_manager._server_instances = {}
    rm = make_runtime_manager(policy_engine)
    notifier = PolicyNotifier(
        session_manager, registry, rm,
        debounce_seconds=debounce_seconds,
    )
    return notifier, registry, session_manager


@pytest.mark.asyncio
async def test_notify_role_changed_sends_to_affected_users() -> None:
    notifier, registry, sm = make_notifier(
        users={
            "alice@test.com": UserDefinition(role="viewer"),
            "bob@test.com": UserDefinition(role="admin"),
        },
        roles={
            "viewer": RoleDefinition(settings=RoleSettings()),
            "admin": RoleDefinition(is_admin=True, settings=RoleSettings()),
        },
    )

    alice_transport = make_mock_transport()
    bob_transport = make_mock_transport()
    sm._server_instances["s1"] = alice_transport
    sm._server_instances["s2"] = bob_transport

    registry.register("s1", DEFAULT_ORG_ID, "alice@test.com")
    registry.register("s2", DEFAULT_ORG_ID, "bob@test.com")

    notifier.notify_role_changed(DEFAULT_ORG_ID, "viewer")
    await asyncio.sleep(0.1)

    alice_transport._write_stream.send_nowait.assert_called_once()
    bob_transport._write_stream.send_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_notify_user_changed_sends_only_to_that_user() -> None:
    notifier, registry, sm = make_notifier(
        users={"alice@test.com": UserDefinition(role="viewer")},
    )

    transport = make_mock_transport()
    sm._server_instances["s1"] = transport
    registry.register("s1", DEFAULT_ORG_ID, "alice@test.com")

    notifier.notify_user_changed(DEFAULT_ORG_ID, "alice@test.com")
    await asyncio.sleep(0.1)

    transport._write_stream.send_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_debounce_collapses_rapid_changes() -> None:
    notifier, registry, sm = make_notifier(
        users={"alice@test.com": UserDefinition(role="viewer")},
        debounce_seconds=0.1,
    )

    transport = make_mock_transport()
    sm._server_instances["s1"] = transport
    registry.register("s1", DEFAULT_ORG_ID, "alice@test.com")

    # Fire 5 rapid changes
    for _ in range(5):
        notifier.notify_role_changed(DEFAULT_ORG_ID, "viewer")

    # Wait for debounce to fire
    await asyncio.sleep(0.2)

    # Should only send once despite 5 changes
    transport._write_stream.send_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_stale_session_cleaned_up() -> None:
    notifier, registry, _sm = make_notifier(
        users={"alice@test.com": UserDefinition(role="viewer")},
    )

    # Register session but don't add transport to session_manager
    registry.register("s1", DEFAULT_ORG_ID, "alice@test.com")

    notifier.notify_role_changed(DEFAULT_ORG_ID, "viewer")
    await asyncio.sleep(0.1)

    # Session should be unregistered since transport was not found
    assert registry.get_session_ids_for_user(DEFAULT_ORG_ID, "alice@test.com") == []


@pytest.mark.asyncio
async def test_notify_all_roles_broadcasts() -> None:
    notifier, registry, sm = make_notifier(
        users={
            "alice@test.com": UserDefinition(role="viewer"),
            "bob@test.com": UserDefinition(role="admin"),
        },
        roles={
            "viewer": RoleDefinition(settings=RoleSettings()),
            "admin": RoleDefinition(is_admin=True, settings=RoleSettings()),
        },
    )

    t1 = make_mock_transport()
    t2 = make_mock_transport()
    sm._server_instances["s1"] = t1
    sm._server_instances["s2"] = t2
    registry.register("s1", DEFAULT_ORG_ID, "alice@test.com")
    registry.register("s2", DEFAULT_ORG_ID, "bob@test.com")

    notifier.notify_all_roles()
    await asyncio.sleep(0.1)

    t1._write_stream.send_nowait.assert_called_once()
    t2._write_stream.send_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_no_sessions_does_not_error() -> None:
    notifier, _registry, _sm = make_notifier(
        users={"alice@test.com": UserDefinition(role="viewer")},
    )

    # No sessions registered — should not raise
    notifier.notify_role_changed(DEFAULT_ORG_ID, "viewer")
    await asyncio.sleep(0.1)


def make_notifier_with_tool_registry(
    debounce_seconds: float = 0.05,
) -> tuple[PolicyNotifier, GatewaySessionRegistry, MagicMock, MagicMock]:
    """Build a notifier whose runtime exposes a mocked ToolRegistry.

    Returns (notifier, registry, session_manager, tool_registry).
    """
    config = make_config(users={"alice@test.com": UserDefinition(role="viewer")})
    policy_engine = PolicyEngine(config)
    registry = GatewaySessionRegistry()
    session_manager = MagicMock()
    session_manager._server_instances = {}
    tool_registry = MagicMock()
    tool_registry.refresh_upstream = AsyncMock()
    rm = make_runtime_manager(policy_engine, tool_registry=tool_registry)
    notifier = PolicyNotifier(
        session_manager, registry, rm, debounce_seconds=debounce_seconds,
    )
    return notifier, registry, session_manager, tool_registry


@pytest.mark.asyncio
async def test_notify_upstream_tools_refreshes_cache_and_broadcasts_org() -> None:
    notifier, registry, sm, tool_registry = make_notifier_with_tool_registry()

    t_own = make_mock_transport()
    t_other_org = make_mock_transport()
    sm._server_instances["s_own"] = t_own
    sm._server_instances["s_other"] = t_other_org
    registry.register("s_own", DEFAULT_ORG_ID, "alice@test.com")
    registry.register("s_other", "other-org", "alice@test.com")

    notifier.notify_upstream_tools_changed(DEFAULT_ORG_ID, "github")
    # Wait for debounce + async refresh task
    await asyncio.sleep(0.2)

    tool_registry.refresh_upstream.assert_awaited_once_with("github")
    t_own._write_stream.send_nowait.assert_called_once()
    t_other_org._write_stream.send_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_notify_upstream_tools_debounces_bursts() -> None:
    notifier, registry, sm, tool_registry = make_notifier_with_tool_registry(
        debounce_seconds=0.1,
    )

    t = make_mock_transport()
    sm._server_instances["s1"] = t
    registry.register("s1", DEFAULT_ORG_ID, "alice@test.com")

    for _ in range(5):
        notifier.notify_upstream_tools_changed(DEFAULT_ORG_ID, "github")

    await asyncio.sleep(0.2)

    # Burst of 5 collapses to one refresh + one broadcast.
    assert tool_registry.refresh_upstream.await_count == 1
    t._write_stream.send_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_notify_upstream_tools_refresh_failure_still_broadcasts() -> None:
    notifier, registry, sm, tool_registry = make_notifier_with_tool_registry()
    tool_registry.refresh_upstream.side_effect = RuntimeError("boom")

    t = make_mock_transport()
    sm._server_instances["s1"] = t
    registry.register("s1", DEFAULT_ORG_ID, "alice@test.com")

    notifier.notify_upstream_tools_changed(DEFAULT_ORG_ID, "github")
    await asyncio.sleep(0.2)

    # Clients still notified so they can re-list and recover.
    t._write_stream.send_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_notify_upstream_tools_unknown_org_noops() -> None:
    notifier, _registry, _sm, tool_registry = make_notifier_with_tool_registry()

    notifier.notify_upstream_tools_changed("nonexistent-org", "github")
    await asyncio.sleep(0.2)

    tool_registry.refresh_upstream.assert_not_awaited()


# ── Multi-org sentinel sweep ─────────────────────────────────────────
#
# In cloud mode the user MCP gateway lives at the fixed ``/mcp`` URL
# and sessions register with ``MULTI_ORG_SENTINEL`` (no slug in the
# URL). Without the sentinel sweep, every notify_* helper that filters
# by ``(real_org_id, ...)`` silently misses those sessions — role
# toggles, individual-tool toggles, user mutations, and upstream
# tools/resources/prompts changes all flow through the same plumbing,
# so one bug → every policy mutation goes nowhere on the cloud
# gateway.


@pytest.mark.asyncio
async def test_notify_role_changed_reaches_multi_org_cloud_session() -> None:
    """A role mutation in org X must reach a session that user X has
    open against the cloud ``/mcp`` endpoint, not just sessions on the
    slug-scoped admin MCP."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    notifier, registry, sm = make_notifier(
        users={"alice@test.com": UserDefinition(role="viewer")},
    )

    cloud_t = make_mock_transport()
    sm._server_instances["s-cloud"] = cloud_t
    # Cloud /mcp session — registered with the sentinel.
    registry.register("s-cloud", MULTI_ORG_SENTINEL, "alice@test.com")

    notifier.notify_role_changed(DEFAULT_ORG_ID, "viewer")
    await asyncio.sleep(0.1)

    cloud_t._write_stream.send_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_notify_user_changed_reaches_multi_org_cloud_session() -> None:
    """Same shape as the role test, but for user-scoped mutations
    (e.g. role assignment, user removed from org)."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    notifier, registry, sm = make_notifier(
        users={"alice@test.com": UserDefinition(role="viewer")},
    )

    cloud_t = make_mock_transport()
    sm._server_instances["s-cloud"] = cloud_t
    registry.register("s-cloud", MULTI_ORG_SENTINEL, "alice@test.com")

    notifier.notify_user_changed(DEFAULT_ORG_ID, "alice@test.com")
    await asyncio.sleep(0.1)

    cloud_t._write_stream.send_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_notify_role_changed_does_not_reach_unaffected_cloud_user() -> None:
    """The sentinel sweep is *user*-bounded: a cloud session for some
    OTHER user should not get notified by a role mutation that only
    targets ``alice``. Otherwise every cloud session would refresh on
    every policy mutation in any org, regardless of membership."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    notifier, registry, sm = make_notifier(
        users={
            "alice@test.com": UserDefinition(role="viewer"),
            "bob@test.com": UserDefinition(role="admin"),
        },
        roles={
            "viewer": RoleDefinition(settings=RoleSettings()),
            "admin": RoleDefinition(is_admin=True, settings=RoleSettings()),
        },
    )

    alice_cloud = make_mock_transport()
    bob_cloud = make_mock_transport()
    sm._server_instances["s-alice"] = alice_cloud
    sm._server_instances["s-bob"] = bob_cloud
    registry.register("s-alice", MULTI_ORG_SENTINEL, "alice@test.com")
    registry.register("s-bob", MULTI_ORG_SENTINEL, "bob@test.com")

    # Mutate the viewer role — only alice's session should refresh.
    notifier.notify_role_changed(DEFAULT_ORG_ID, "viewer")
    await asyncio.sleep(0.1)

    alice_cloud._write_stream.send_nowait.assert_called_once()
    bob_cloud._write_stream.send_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_notify_upstream_tools_reaches_multi_org_cloud_session() -> None:
    """An upstream-side ``tools/list_changed`` for org X must reach
    the cloud session of any user who's a member of X — even though
    that session is registered under the multi-org sentinel."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    notifier, registry, sm, tool_registry = make_notifier_with_tool_registry()

    cloud_t = make_mock_transport()
    sm._server_instances["s-cloud"] = cloud_t
    # Alice is configured under the org's policy_engine (see
    # make_notifier_with_tool_registry → users={alice...}). Her cloud
    # session is registered with the sentinel.
    registry.register("s-cloud", MULTI_ORG_SENTINEL, "alice@test.com")

    notifier.notify_upstream_tools_changed(DEFAULT_ORG_ID, "github")
    await asyncio.sleep(0.2)

    tool_registry.refresh_upstream.assert_awaited_once_with("github")
    cloud_t._write_stream.send_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_notify_upstream_tools_skips_non_member_cloud_session() -> None:
    """Defense-in-depth: the upstream-changed sweep must use the org's
    policy member set, not 'every cloud session'. A user not configured
    on the affected org should NOT be notified."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    notifier, registry, sm, _tool_registry = make_notifier_with_tool_registry()

    # alice is the only user in DEFAULT_ORG_ID's policy. carol is some
    # other tenant's user with an unrelated cloud session.
    carol_cloud = make_mock_transport()
    sm._server_instances["s-carol"] = carol_cloud
    registry.register("s-carol", MULTI_ORG_SENTINEL, "carol@other.com")

    notifier.notify_upstream_tools_changed(DEFAULT_ORG_ID, "github")
    await asyncio.sleep(0.2)

    carol_cloud._write_stream.send_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_terminate_user_sessions_reaches_multi_org_cloud_session() -> None:
    """Removing a user from an org has to tear down every gateway
    session that user owns — including their multi-org cloud session.
    Otherwise the user keeps an open ``/mcp`` connection serving tools
    from an org they're no longer in."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    notifier, registry, sm = make_notifier(
        users={"alice@test.com": UserDefinition(role="viewer")},
    )

    sm._server_instances["s-cloud"] = make_mock_transport()
    sm._server_instances["s-admin"] = make_mock_transport()
    registry.register("s-cloud", MULTI_ORG_SENTINEL, "alice@test.com")
    registry.register("s-admin", DEFAULT_ORG_ID, "alice@test.com")

    removed = notifier.terminate_user_sessions(
        DEFAULT_ORG_ID, "alice@test.com",
    )
    # Both — admin-mounted AND cloud-mounted — should be popped.
    assert removed == 2
    assert "s-cloud" not in sm._server_instances
    assert "s-admin" not in sm._server_instances


class _FakeRecoveryManager:
    """The slice of ``UpstreamClientManager`` that the recovery wrapper +
    ``acquire_upstream_session`` touch for a service_account upstream — lets
    the notifier-recovery test count fresh reconnects without a real sandbox."""

    def __init__(self) -> None:
        self.ensure_calls = 0
        self.fresh_calls = 0

    async def ensure_shared_connected(self, upstream: Any) -> None:
        self.ensure_calls += 1

    def get_session(self, upstream_id: str, user_id: str | None = None) -> Any:
        return object()

    async def reconnect_shared_fresh(self, upstream: Any) -> None:
        self.fresh_calls += 1


def make_recovery_notifier(
    refresh_behaviours: list[Any],
    debounce_seconds: float = 0.05,
) -> tuple[
    PolicyNotifier, GatewaySessionRegistry, MagicMock, AsyncMock,
    _FakeRecoveryManager,
]:
    """A notifier whose service_account upstream ``mee6`` refreshes by walking
    *refresh_behaviours* (an exception is raised; anything else is returned).

    Returns (notifier, registry, session_manager, refresh_upstream_mock,
    client_manager)."""
    config = make_config(users={"alice@test.com": UserDefinition(role="viewer")})
    policy_engine = PolicyEngine(config)
    registry = GatewaySessionRegistry()
    session_manager = MagicMock()
    session_manager._server_instances = {}
    tool_registry = MagicMock()
    refresh_mock = AsyncMock(side_effect=refresh_behaviours)
    tool_registry.refresh_upstream = refresh_mock
    client_manager = _FakeRecoveryManager()
    upstream = make_upstream_definition(id="mee6")  # default: service_account
    rm = make_runtime_manager(
        policy_engine,
        tool_registry=tool_registry,
        client_manager=cast(Any, client_manager),
        upstreams=[upstream],
    )
    notifier = PolicyNotifier(
        session_manager, registry, rm,
        debounce_seconds=debounce_seconds,
    )
    return notifier, registry, session_manager, refresh_mock, client_manager


@pytest.mark.asyncio
async def test_notify_upstream_tools_recovers_from_transport_stall() -> None:
    # A service_account upstream's first refresh hits a transport stall (the
    # E2B post-reattach stdout stall, surfaced as asyncio.TimeoutError). The
    # notifier must drop the stalled session, reconnect FRESH, and retry —
    # not log refresh_after_change.failed and abandon the session (MCPOLIS-BACKEND-P).
    notifier, registry, sm, refresh_mock, client_manager = make_recovery_notifier(
        [asyncio.TimeoutError(), []],
    )

    t = make_mock_transport()
    sm._server_instances["s1"] = t
    registry.register("s1", DEFAULT_ORG_ID, "alice@test.com")

    notifier.notify_upstream_tools_changed(DEFAULT_ORG_ID, "mee6")
    await asyncio.sleep(0.2)

    assert client_manager.fresh_calls == 1, "must force a fresh reconnect on stall"
    assert refresh_mock.await_count == 2, "must retry the refresh after the stall"
    t._write_stream.send_nowait.assert_called_once()
