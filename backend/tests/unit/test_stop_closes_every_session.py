"""The dashboard's Stop (Disconnect, for a remote HTTP server) closes
EVERY live session to the server: the shared one and each member's own.
It deletes NO saved sign-in, so after Start nobody signs in again.

Until Start, a stopped server refuses gateway calls, including calls from
members who had a live personal session when the admin clicked Stop, and
it opens no connection to the server for them.

Same harness as ``test_user_disconnect_race.py``: a REAL streamable-HTTP
MCP server on loopback whose per-connection startup hook can hold a
connect in flight, a REAL file-backed token store, the REAL reconnect path.
"""
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import structlog
from mcp.client.session import ClientSession
from mcp.types import TextContent

from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.repositories.file_upstream_config_store import (
    FileUpstreamConfigStore,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
    UpstreamStopped,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.settings import (
    RoleDefinition,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.model.upstream import TransportType, UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.services import upstream_connection_service
from mcpolis.domain.services.upstream_config_service import (
    UpstreamConfigService,
)
from mcpolis.domain.services.upstream_connection_service import (
    UPSTREAM_STOPPED,
    SessionUnavailable,
    reopen_stopped_upstream,
    start_from_saved_sign_in,
    start_shared_in_background,
    stop_keeping_sign_ins,
)
from mcpolis.entrypoints.controllers.admin_mcp_controller import (
    create_admin_mcp_server,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_user_id,
)
from tests.unit._user_session_harness import (
    ALICE,
    BOB,
    GATEWAY_URL,
    UPSTREAM_ID,
    ConnectionGate,
    acquire,
    make_store,
    make_token,
    make_upstream,
    start_upstream,
    stop_upstream as stop_test_server,
    wait_until,
)
from tests.unit.factories import (
    make_discovered_tool,
    make_oauth_upstream,
    make_runtime_manager,
    make_upstream_definition,
)


async def whoami(session: ClientSession) -> str:
    """The bearer the session's connection was opened with."""
    result = await session.call_tool("whoami", {})
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def stop(
    mgr: UpstreamClientManager, store: FileConnectionStore,
) -> None:
    """The admin's Stop, as the dashboard and the Admin MCP run it."""
    await stop_keeping_sign_ins(
        org_id=DEFAULT_ORG_ID,
        upstream_id=UPSTREAM_ID,
        client_manager=mgr,
        connection_store=store,
    )


def make_router(
    upstream: UpstreamDefinition,
    mgr: UpstreamClientManager,
    store: FileConnectionStore,
    tmp_path: Path,
) -> ToolRouter:
    """The gateway's tool router, over the real manager and store, with
    the test server's ``echo`` and ``hold`` tools in the catalog."""
    registry = ToolRegistry([upstream], mgr)
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(upstream_id=UPSTREAM_ID, original_name="echo"),
        make_discovered_tool(upstream_id=UPSTREAM_ID, original_name="hold"),
    ]
    policy = PolicyEngine(SettingsConfig(
        roles={"admin": RoleDefinition(is_admin=True)},
        users={ALICE: UserDefinition(role="admin")},
    ))
    return ToolRouter(
        registry, mgr, FileAuditRepository(tmp_path / "audit.jsonl"),
        [upstream], policy_engine=policy, connection_store=store,
        server_url=GATEWAY_URL,
    )


async def call_tool(
    router: ToolRouter, user: str, tool: str, arguments: dict[str, Any],
) -> tuple[bool, str]:
    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name=f"{UPSTREAM_ID}__{tool}",
        arguments=arguments,
        user_id=user,
        session_id=None,
    )
    block = result.content[0]
    assert isinstance(block, TextContent)
    return bool(result.isError), block.text


async def call_echo(router: ToolRouter, user: str) -> tuple[bool, str]:
    return await call_tool(router, user, "echo", {"message": "hi"})


class SlowStore(FileConnectionStore):
    """A real file store whose Stop writes take a while, like a slow
    database. Clearing the error waits before writing; saving the stop
    writes, then waits before returning."""

    async def clear_connection_error(self, org_id: str, upstream_id: str) -> None:
        await asyncio.sleep(0.2)
        await super().clear_connection_error(org_id, upstream_id)

    async def set_disabled(self, org_id: str, upstream_id: str) -> None:
        await super().set_disabled(org_id, upstream_id)
        await asyncio.sleep(0.2)


async def make_slow_store(tmp_path: Path, tokens: dict[str, str]) -> SlowStore:
    store = SlowStore(tmp_path)
    for user, access_token in tokens.items():
        await store.put_user_token(
            DEFAULT_ORG_ID, user, UPSTREAM_ID, make_token(access_token),
        )
    return store


@pytest.mark.asyncio
async def test_stop_closes_every_members_session(tmp_path: Path) -> None:
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        await acquire(mgr, upstream, store, ALICE)
        await acquire(mgr, upstream, store, BOB)

        await stop(mgr, store)

        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
        assert not mgr.has_user_session(UPSTREAM_ID, BOB), (
            "a member's personal session survived Stop"
        )
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_stop_keeps_every_saved_sign_in(tmp_path: Path) -> None:
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        await acquire(mgr, upstream, store, ALICE)

        await stop(mgr, store)

        for user in (ALICE, BOB):
            token = await store.get_user_token(DEFAULT_ORG_ID, user, UPSTREAM_ID)
            assert token is not None, f"Stop deleted {user}'s saved sign-in"
        assert not await store.is_enabled(DEFAULT_ORG_ID, UPSTREAM_ID), (
            "Stop must survive a restart"
        )
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_a_member_call_after_stop_is_refused_without_connecting(
    tmp_path: Path,
) -> None:
    """Bob still has a saved sign-in. His next call must not reconnect
    from it: the server is stopped until an admin's Start."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        await acquire(mgr, upstream, store, BOB)
        await stop(mgr, store)
        opened_before = gate.opened

        with pytest.raises(SessionUnavailable) as refused:
            await acquire(mgr, upstream, store, BOB)

        assert refused.value.reason == UPSTREAM_STOPPED
        assert gate.opened == opened_before, (
            "a call on a stopped server opened a connection to it"
        )
        assert not mgr.has_user_session(UPSTREAM_ID, BOB)
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_stop_ends_a_member_connect_still_running(tmp_path: Path) -> None:
    """Bob's call is reconnecting him from his saved sign-in when the
    admin clicks Stop. The connect must not land a session after Stop."""
    gate = ConnectionGate(hold={2})  # hold the connect, after its probe
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store, BOB))
        await wait_until(lambda: gate.opened >= 2)

        await stop(mgr, store)
        gate.release.set()
        outcome = await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )
        await asyncio.sleep(0.5)  # room for a connect that was not stopped

        assert not mgr.has_user_session(UPSTREAM_ID, BOB), (
            "a session landed after Stop"
        )
        assert isinstance(outcome[0], SessionUnavailable), outcome
        assert outcome[0].reason == UPSTREAM_STOPPED, (
            "an interrupted member is told to sign in again, "
            "although only an admin's Start helps"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_the_gateway_tells_a_member_the_server_is_unavailable(
    tmp_path: Path,
) -> None:
    """The refusal points at the admins. "You are not signed in" would be
    wrong: Bob is signed in, and signing in again would not help."""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    router = make_router(upstream, mgr, store, tmp_path)
    try:
        is_error, _ = await call_echo(router, BOB)
        assert not is_error

        await stop(mgr, store)
        is_error, text = await call_echo(router, BOB)

        assert is_error
        assert "not currently available" in text, text
        assert ALICE in text, "the refusal must name the admins"
        assert not mgr.has_user_session(UPSTREAM_ID, BOB)
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_a_shared_sign_in_server_refuses_calls_after_stop(
    tmp_path: Path,
) -> None:
    """Shared sign-in (admin_oauth): every call runs on the admin's saved
    sign-in. Stop keeps that sign-in, so the call must be refused by the
    stop itself, not by a missing sign-in."""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_oauth_upstream(
        id=UPSTREAM_ID, display_name="Drop", mode=AuthMode.admin_oauth,
        url=url,
    )
    store = await make_store(tmp_path, {ALICE: "token-a"})
    mgr = UpstreamClientManager([upstream])
    router = make_router(upstream, mgr, store, tmp_path)
    try:
        is_error, _ = await call_echo(router, BOB)
        assert not is_error

        await stop(mgr, store)
        is_error, text = await call_echo(router, BOB)

        assert is_error
        assert "not currently available" in text, text
        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_after_start_every_member_reconnects_from_their_saved_sign_in(
    tmp_path: Path,
) -> None:
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        await acquire(mgr, upstream, store, BOB)
        await stop(mgr, store)

        await reopen_stopped_upstream(
            org_id=DEFAULT_ORG_ID,
            upstream_id=UPSTREAM_ID,
            client_manager=mgr,
            connection_store=store,
        )

        assert await store.is_enabled(DEFAULT_ORG_ID, UPSTREAM_ID)
        bob = await acquire(mgr, upstream, store, BOB)
        assert await whoami(bob) == "Bearer token-b"
        alice = await acquire(mgr, upstream, store, ALICE)
        assert await whoami(alice) == "Bearer token-a"
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_start_reconnects_the_admin_from_the_saved_sign_in(
    tmp_path: Path,
) -> None:
    """Start on a stopped server whose admin sign-in was kept: no sign-in
    page, the admin's session is back, the server is no longer stopped."""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a"})
    mgr = UpstreamClientManager([upstream])
    registry = ToolRegistry([upstream], mgr)
    try:
        await acquire(mgr, upstream, store, ALICE)
        await stop(mgr, store)

        result = await start_from_saved_sign_in(
            org_id=DEFAULT_ORG_ID,
            upstream=upstream,
            owner=ALICE,
            connection_store=store,
            client_manager=mgr,
            tool_registry=registry,
            server_url=GATEWAY_URL,
        )

        assert result.connected, result.error
        assert result.authorization_url is None
        assert not mgr.is_stopped(UPSTREAM_ID)
        assert mgr.has_user_session(UPSTREAM_ID, ALICE)
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_a_start_during_a_stop_leaves_memory_and_storage_agreeing(
    tmp_path: Path,
) -> None:
    """An admin clicks Start while another admin's Stop is still being
    saved. Whichever wins, the running app and the saved state must say
    the same thing, or the next restart flips the server."""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_slow_store(tmp_path, {ALICE: "token-a"})
    mgr = UpstreamClientManager([upstream])
    try:
        stopping = asyncio.create_task(stop(mgr, store))
        await asyncio.sleep(0.05)  # the Stop is partway through
        await reopen_stopped_upstream(
            org_id=DEFAULT_ORG_ID,
            upstream_id=UPSTREAM_ID,
            client_manager=mgr,
            connection_store=store,
        )
        await asyncio.wait_for(stopping, timeout=10)

        saved_stopped = not await store.is_enabled(DEFAULT_ORG_ID, UPSTREAM_ID)
        assert mgr.is_stopped(UPSTREAM_ID) == saved_stopped, (
            f"running app stopped={mgr.is_stopped(UPSTREAM_ID)}, "
            f"saved stopped={saved_stopped}"
        )
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


class SlowStartStore(FileConnectionStore):
    """A real file store whose Start write lands, then takes a while to
    return, like a slow database."""

    async def set_enabled(self, org_id: str, upstream_id: str) -> None:
        await super().set_enabled(org_id, upstream_id)
        await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_a_stop_during_a_server_start_leaves_memory_and_storage_agreeing(
    tmp_path: Path,
) -> None:
    """Same race for a server without sign-in, whose Start connects in
    the background: a Stop landing while Start saves must not leave it
    running while storage says stopped."""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream_definition(
        id=UPSTREAM_ID, transport=TransportType.streamable_http, url=url,
    )
    store = SlowStartStore(tmp_path)
    mgr = UpstreamClientManager([upstream])
    try:
        await stop(mgr, store)

        async def start() -> asyncio.Task[None]:
            # Like the admin's Start, which holds the Stop/Start lock from
            # its checks through the launch.
            async with mgr.stop_start_lock(UPSTREAM_ID):
                return await start_shared_in_background(
                    org_id=DEFAULT_ORG_ID,
                    upstream_id=UPSTREAM_ID,
                    client_manager=mgr,
                    connection_store=store,
                    connect=lambda: mgr.connect_upstream(upstream),
                )

        starting = asyncio.create_task(start())
        await asyncio.sleep(0.05)  # Start's write landed, its return is slow
        await stop(mgr, store)
        await asyncio.wait_for(starting, timeout=10)
        await wait_until(lambda: not mgr.is_starting(UPSTREAM_ID))

        saved_stopped = not await store.is_enabled(DEFAULT_ORG_ID, UPSTREAM_ID)
        assert mgr.is_stopped(UPSTREAM_ID) == saved_stopped, (
            f"running app stopped={mgr.is_stopped(UPSTREAM_ID)}, "
            f"saved stopped={saved_stopped}"
        )
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


class SandboxCleanupFailsManager(UpstreamClientManager):
    """A Stop that fails after the app stopped the upstream: the sandbox
    clean-up at its end errors (for example, the provider API is down)."""

    async def kill_persisted_session_for_upstream(
        self, upstream_id: str,
    ) -> None:
        del upstream_id
        raise RuntimeError("sandbox API down")


@pytest.mark.asyncio
async def test_a_stop_that_fails_late_stays_saved_stopped(tmp_path: Path) -> None:
    """The app already refuses calls when the clean-up fails, so storage
    must say stopped too, or the next restart starts it again."""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a"})
    mgr = SandboxCleanupFailsManager([upstream])
    try:
        with pytest.raises(RuntimeError, match="sandbox API down"):
            await stop(mgr, store)

        assert mgr.is_stopped(UPSTREAM_ID)
        assert not await store.is_enabled(DEFAULT_ORG_ID, UPSTREAM_ID)
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_a_stop_during_a_start_ends_the_start_cleanly(
    tmp_path: Path,
) -> None:
    """A Stop lands while Start reconnects the admin from the kept
    sign-in. Start reports it did not connect (no crash, which the
    dashboard would show as a server error), and the server stays
    stopped, in the running app and in storage."""
    gate = ConnectionGate(hold={4})  # Start's connect, after its probe
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a"})
    mgr = UpstreamClientManager([upstream])
    registry = ToolRegistry([upstream], mgr)
    try:
        await acquire(mgr, upstream, store, ALICE)  # connections 1 and 2
        await stop(mgr, store)
        starting = asyncio.create_task(start_from_saved_sign_in(
            org_id=DEFAULT_ORG_ID,
            upstream=upstream,
            owner=ALICE,
            connection_store=store,
            client_manager=mgr,
            tool_registry=registry,
            server_url=GATEWAY_URL,
        ))
        await wait_until(lambda: gate.opened >= 4)

        await stop(mgr, store)
        gate.release.set()
        result = await asyncio.wait_for(starting, timeout=30)

        assert not result.connected
        assert result.aborted
        assert mgr.is_stopped(UPSTREAM_ID)
        assert not await store.is_enabled(DEFAULT_ORG_ID, UPSTREAM_ID)
        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_a_call_cut_off_by_stop_says_stopped_and_raises_no_alert(
    tmp_path: Path,
) -> None:
    """Bob's tool call is running when the admin clicks Stop. He gets the
    "contact an administrator" answer, and nothing is logged as an error
    (errors page the operator through Sentry; a Stop is not a fault)."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    router = make_router(upstream, mgr, store, tmp_path)
    try:
        with structlog.testing.capture_logs() as logs:
            call = asyncio.create_task(call_tool(router, BOB, "hold", {}))
            await asyncio.wait_for(gate.tool_entered.wait(), timeout=10)

            await stop(mgr, store)
            is_error, text = await asyncio.wait_for(call, timeout=30)

        assert is_error
        assert "not currently available" in text, text
        errors = [e for e in logs if e.get("log_level") in ("error", "critical")]
        assert errors == [], f"a Stop raised error events: {errors}"
    finally:
        gate.tool_release.set()
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_a_stopped_server_never_hands_out_a_recorded_session(
    tmp_path: Path,
) -> None:
    """Whatever session is still recorded for a stopped server, a call
    never gets it: it is refused like any call on a stopped server."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        await acquire(mgr, upstream, store, BOB)
        mgr.mark_saved_stops([UPSTREAM_ID])  # stopped, session still recorded
        opened_before = gate.opened

        with pytest.raises(SessionUnavailable) as refused:
            await acquire(mgr, upstream, store, BOB)

        assert refused.value.reason == UPSTREAM_STOPPED
        assert gate.opened == opened_before
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_the_check_up_after_a_stall_is_quiet_on_a_stopped_server(
    tmp_path: Path,
) -> None:
    """After a stalled call, the gateway reconnects the user once to
    check their sign-in. On a stopped server that reconnect is refused,
    which is expected: it must not be logged as an error."""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        await stop(mgr, store)

        with structlog.testing.capture_logs() as logs:
            await upstream_connection_service.settle_oauth_state_after_stall(
                org_id=DEFAULT_ORG_ID,
                upstream=upstream,
                effective_user=BOB,
                connection_store=store,
                client_manager=mgr,
                server_url=GATEWAY_URL,
            )

        errors = [e for e in logs if e.get("log_level") in ("error", "critical")]
        assert errors == [], f"a Stop raised error events: {errors}"
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_a_fresh_sign_in_is_refused_on_a_stopped_server(
    tmp_path: Path,
) -> None:
    """A deliberate new sign-in (it replaces the user's session) opens no
    connection on a stopped server either."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a"})
    mgr = UpstreamClientManager([upstream])
    try:
        await stop(mgr, store)
        opened_before = gate.opened

        with pytest.raises(UpstreamStopped):
            await mgr.replace_user_session(
                upstream, ALICE, bearer_token="token-new",
            )

        assert gate.opened == opened_before
        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)


@pytest.mark.asyncio
async def test_stop_lets_a_token_refresh_in_progress_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop cancels each member's running reconnect. If that lands in the
    middle of a token refresh, after the provider issued new tokens but
    before they are saved, the saved sign-in is dead and the member must
    sign in again after Start. So the refresh itself must finish."""
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow_refresh(oauth_auth: object, url: str) -> None:
        del oauth_auth, url
        entered.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr(
        upstream_connection_service, "_trigger_silent_refresh", slow_refresh,
    )
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store, BOB))
        await asyncio.wait_for(entered.wait(), timeout=10)

        await stop(mgr, store)
        release.set()

        await asyncio.wait_for(finished.wait(), timeout=5)
        outcome = await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )
        assert isinstance(outcome[0], SessionUnavailable), outcome
    finally:
        release.set()
        await mgr.stop_all()
        await stop_test_server(server, server_task)


# ---------- the Admin MCP's disconnect_upstream tool ----------


async def make_admin_mcp(
    tmp_path: Path, upstream: UpstreamDefinition,
    mgr: UpstreamClientManager, store: FileConnectionStore,
) -> Any:
    """The Admin MCP server, over the real manager and store."""
    assert upstream.http is not None
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {UPSTREAM_ID: {"url": upstream.http.url}},
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "upstreams": {
            UPSTREAM_ID: {
                "display_name": "Drop", "auth_mode": "per_user_oauth",
            },
        },
        "roles": {"admin": {"is_admin": True}, "user": {"is_default": True}},
        "users": {ALICE: {"role": "admin"}},
    }))
    config_store = FileConfigStore(config_path)
    policy_engine = PolicyEngine(config_store.ensure_defaults_sync(DEFAULT_ORG_ID))
    registry = ToolRegistry([upstream], mgr)
    config_service = UpstreamConfigService(
        FileUpstreamConfigStore(McpJsonStore(mcp_json), config_store),
        mgr, registry, store,
        config_repo=config_store, policy_engine=policy_engine,
    )
    runtime_manager = make_runtime_manager(
        policy_engine,
        tool_registry=registry,
        client_manager=mgr,
        config_service=config_service,
    )
    return create_admin_mcp_server(
        runtime_manager=runtime_manager,
        audit_repo=FileAuditRepository(tmp_path / "audit.jsonl"),
        policy_store=config_store,
        template_var_repo=FileTemplateVarRepository(tmp_path),
        connection_store=store,
    )


@pytest.mark.asyncio
async def test_the_admin_mcp_disconnect_stops_like_the_dashboard(
    tmp_path: Path,
) -> None:
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    admin_mcp = await make_admin_mcp(tmp_path, upstream, mgr, store)
    try:
        await acquire(mgr, upstream, store, ALICE)
        await acquire(mgr, upstream, store, BOB)

        org_token = current_org_id.set(DEFAULT_ORG_ID)
        user_token = current_user_id.set(ALICE)
        try:
            await admin_mcp.call_tool(
                "disconnect_upstream", {"mcp_id": UPSTREAM_ID},
            )
        finally:
            current_org_id.reset(org_token)
            current_user_id.reset(user_token)

        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
        assert not mgr.has_user_session(UPSTREAM_ID, BOB)
        for user in (ALICE, BOB):
            token = await store.get_user_token(DEFAULT_ORG_ID, user, UPSTREAM_ID)
            assert token is not None, f"Stop deleted {user}'s saved sign-in"
        with pytest.raises(SessionUnavailable) as refused:
            await acquire(mgr, upstream, store, BOB)
        assert refused.value.reason == UPSTREAM_STOPPED
    finally:
        await mgr.stop_all()
        await stop_test_server(server, server_task)
