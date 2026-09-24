"""Requests that need the same user's session at the same moment must share
ONE reconnect, and nothing that rebuilds or heals a session may destroy a
fresh one that somebody else just built.

Sentry MCPOLIS-BACKEND-W (2026-09-23, upstream ``drop``): two tool calls
from one user arrived 70 ms apart with no live session. Each ran a full
reconnect from stored tokens. The second one's connect began by closing
"the existing session", which was the session the first request had built
a moment earlier and was about to use. The first request's lookup then
found nothing and raised ``KeyError``.

These tests replay that shape against a REAL streamable-HTTP MCP server on
loopback, a REAL file-backed connection store holding the user's tokens,
and the REAL reconnect path (OAuth provider, token probe, HTTP connect).
Nothing inside the reconnect is stubbed. The one control knob is the test
server's per-connection startup hook: it can hold a chosen connection
open, which parks a connect in flight with no sleeps.

The server numbers connections in arrival order. One reconnect makes two:
the token probe first, then the real MCP connection. So "one reconnect"
is ``gate.opened == 2`` and two racing reconnects show up as 4.

NOTE: no ``from __future__ import annotations`` — FastMCP tool
registration calls ``issubclass()`` on annotations.
"""
import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest
import structlog
import uvicorn
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as StoredToken,
)
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    USER_SESSION_IDLE_TIMEOUT,
    UpstreamClientManager,
)
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import ToolAnnotations, UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services import tool_router as tool_router_module
from mcpolis.domain.services.oauth_liveness import probe_upstream_liveness
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.services.upstream_connection_service import (
    SessionUnavailable,
    heal_stalled_session,
    try_connect_with_stored_tokens,
)
from tests.unit._state_seed import seed_user_session
from tests.unit._user_session_harness import (
    ALICE,
    BOB,
    UPSTREAM_ID,
    GATEWAY_URL,
    ConnectionGate,
    make_upstream_server,
    start_upstream,
    wait_until_serving,
    stop_upstream,
    make_upstream,
    make_token,
    make_store,
    acquire,
    wait_until,
)
from tests.unit.factories import make_discovered_tool





















async def whoami(session: ClientSession) -> str:
    result = await asyncio.wait_for(
        session.call_tool("whoami", {}), timeout=10,
    )
    assert not result.isError, result
    block = result.content[0]
    assert isinstance(block, mcp_types.TextContent)
    return block.text




async def let_others_run() -> None:
    """Give every task that is ready a turn, so a task created just
    before takes its first step (its synchronous part up to its first
    real wait). Deterministic: no wall-clock sleep."""
    for _ in range(5):
        await asyncio.sleep(0)


def events(
    logs: Sequence[Mapping[str, Any]], name: str,
) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry.get("event") == name]


def make_router(
    tmp_path: Path,
    mgr: UpstreamClientManager,
    upstream: UpstreamDefinition,
    store: FileConnectionStore,
) -> ToolRouter:
    registry = ToolRegistry([upstream], mgr)
    registry._tools = [
        make_discovered_tool(
            upstream_id=UPSTREAM_ID,
            original_name="echo",
            annotations=ToolAnnotations(idempotentHint=True),
        ),
    ]
    return ToolRouter(
        registry,
        mgr,
        FileAuditRepository(tmp_path / "audit.jsonl"),
        [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
        connection_store=store,
        server_url=GATEWAY_URL,
    )


async def call_echo(router: ToolRouter, message: str) -> mcp_types.CallToolResult:
    return await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name=f"{UPSTREAM_ID}__echo",
        arguments={"message": message},
        user_id=ALICE,
        session_id=None,
    )


# --- The Sentry case ------------------------------------------------------


@pytest.mark.asyncio
async def test_two_requests_without_a_session_share_one_reconnect(
    tmp_path: Path,
) -> None:
    """The MCPOLIS-BACKEND-W shape: request A is mid-reconnect when request
    B arrives for the same user. B must wait for A's reconnect and use the
    same session. It must not run a second reconnect, and above all it must
    not close the session A is about to use.

    Oracles, all outcomes: one token probe plus one connection reached the
    server (not two of each), one session was created and none closed,
    both callers hold the SAME session, and that session answers a call.
    One OAuth provider built also means one chance to refresh the token:
    two concurrent refreshes of one rotating refresh token make the loser
    look revoked, and the reconnect then deletes the user's sign-in."""
    gate = ConnectionGate(hold={2})  # hold A's connection, after its probe
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        with structlog.testing.capture_logs() as logs:
            first = asyncio.create_task(acquire(mgr, upstream, store))
            await wait_until(lambda: gate.opened >= 2)
            second = asyncio.create_task(acquire(mgr, upstream, store))
            await let_others_run()
            gate.release.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True),
                timeout=30,
            )

        assert not any(isinstance(r, BaseException) for r in results), (
            f"both requests must get a session; got {results!r}"
        )
        session_a, session_b = results
        assert session_a is session_b, (
            "the second request must use the session the first one built, "
            "not build its own and close the first one's"
        )
        assert gate.opened == 2, (
            f"one reconnect is one probe plus one connection; the server "
            f"saw {gate.opened} connections"
        )
        assert len(events(logs, "upstream.client.user_session.created")) == 1
        assert events(logs, "upstream.client.user_session.closed") == [], (
            "nothing may close a session that was just built for a caller"
        )
        providers = (
            events(logs, "upstream.oauth.metadata.miss")
            + events(logs, "upstream.oauth.metadata.hit")
        )
        assert len(providers) == 1, (
            "one reconnect builds one OAuth provider, so the stored token is "
            f"refreshed at most once; saw {len(providers)} providers"
        )
        assert isinstance(session_a, ClientSession)
        assert await whoami(session_a) == "Bearer token-1"
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_session_torn_down_mid_reconnect_never_raises_a_key_error(
    tmp_path: Path,
) -> None:
    """The exact Sentry frame: the reconnect reports success, then the
    session is gone before the caller looks it up. Production hit this
    when a second request closed it; here the store's first post-connect
    write drops it, which is the same moment in the same code. (A raw
    drop, not a Disconnect: a Disconnect also stops the reconnect, which
    ``test_user_disconnect_race.py`` covers.)

    The tool call must come back as a tool result, never as a raw
    ``KeyError`` out of the router. The tool is marked safe to repeat, so
    the router may recover by reconnecting, and the call succeeds."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    mgr = UpstreamClientManager([upstream])

    class TearsDownDuringHousekeeping(FileConnectionStore):
        torn_down = False

        async def clear_connection_error(
            self, org_id: str, upstream_id: str,
        ) -> None:
            if not self.torn_down:
                self.torn_down = True
                await mgr._drop_user_session(  # pyright: ignore[reportPrivateUsage]
                    (ALICE, upstream_id),
                )
            await super().clear_connection_error(org_id, upstream_id)

    store = TearsDownDuringHousekeeping(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, make_token("token-1"),
    )
    router = make_router(tmp_path, mgr, upstream, store)
    try:
        result = await asyncio.wait_for(call_echo(router, "hi"), timeout=60)
        assert store.torn_down, "precondition: the teardown really happened"
        assert isinstance(result, mcp_types.CallToolResult)
        assert not result.isError, result
        block = result.content[0]
        assert isinstance(block, mcp_types.TextContent)
        assert block.text == "echo:hi"
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


# --- A deliberate re-sign-in still replaces the session --------------------


@pytest.mark.asyncio
async def test_a_re_sign_in_replaces_the_session_with_the_new_token(
    tmp_path: Path,
) -> None:
    """After the user signs in again, the session must be REBUILT on the new
    token. "A session already exists, reuse it" would keep serving the old
    sign-in, which may be revoked or a different account."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        old = await acquire(mgr, upstream, store)
        assert await whoami(old) == "Bearer token-1"

        await store.put_user_token(
            DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, make_token("token-2"),
        )
        outcome = await try_connect_with_stored_tokens(
            DEFAULT_ORG_ID, upstream, ALICE, store, mgr, GATEWAY_URL,
        )
        assert outcome is not None and outcome.connected

        current = await acquire(mgr, upstream, store)
        assert current is not old, "the re-sign-in must build a new session"
        assert await whoami(current) == "Bearer token-2"
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_re_sign_in_during_a_reconnect_still_ends_on_the_new_token(
    tmp_path: Path,
) -> None:
    """Trap: a replacement that JOINS a reconnect already in flight would
    adopt a session opened with the old token. The re-sign-in must wait
    for that reconnect, then build its own, so the user ends on the new
    token."""
    gate = ConnectionGate(hold={2})  # hold the in-flight reconnect's connection
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        in_flight = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)

        await store.put_user_token(
            DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, make_token("token-2"),
        )
        re_sign_in = asyncio.create_task(try_connect_with_stored_tokens(
            DEFAULT_ORG_ID, upstream, ALICE, store, mgr, GATEWAY_URL,
        ))
        await let_others_run()
        gate.release.set()
        _, outcome = await asyncio.wait_for(
            asyncio.gather(in_flight, re_sign_in), timeout=30,
        )
        assert outcome is not None and outcome.connected

        current = await acquire(mgr, upstream, store)
        assert await whoami(current) == "Bearer token-2", (
            "the re-sign-in must win over the reconnect that was in flight"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


# --- Cancellation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_the_first_request_does_not_fail_the_second(
    tmp_path: Path,
) -> None:
    """The request that started a reconnect goes away (its client hung up)
    while another request waits on the same reconnect. The waiter must
    still get the session, and the reconnect must not be run twice."""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        first = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)
        second = asyncio.create_task(acquire(mgr, upstream, store))
        await let_others_run()

        first.cancel()
        await let_others_run()
        gate.release.set()

        session = await asyncio.wait_for(second, timeout=30)
        assert await whoami(session) == "Bearer token-1"
        with pytest.raises(asyncio.CancelledError):
            await first
        assert gate.opened == 2, (
            "the waiter must be served by the reconnect that was already "
            f"running; the server saw {gate.opened} connections"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_lone_request_that_gives_up_stops_its_reconnect(
    tmp_path: Path,
) -> None:
    """Nobody else waits, so giving up must stop the reconnect, the same
    as before the connect was shared: no session appears afterwards."""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        lone = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)
        lone.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lone
        gate.release.set()
        await asyncio.sleep(1.0)  # let an un-stopped connect finish, if any
        assert not mgr.has_user_session(UPSTREAM_ID, ALICE), (
            "a reconnect nobody waits for must not install a session later"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


# --- Heals and sweeps evict only what they observed ------------------------


@pytest.mark.asyncio
async def test_a_heal_that_lost_the_race_leaves_the_fresh_session_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call stalls on a dead session. Before its heal runs, another
    request has already replaced that session with a fresh one. The late
    heal must leave the fresh session alone: evicting "whatever is there
    now" kills a session someone else just built and may be using.

    The dead session is real (the server went away under it). The other
    request's replacement is injected at the heal's doorstep, the one
    moment this race needs."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    port = int(url.rsplit(":", 1)[1].split("/")[0])
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    router = make_router(tmp_path, mgr, upstream, store)

    fresh: list[ClientSession] = []

    async def heal_after_someone_else_replaced(**kwargs: Any) -> None:
        # Another request noticed the same dead session first, evicted it
        # and reconnected. Only then does this call's own heal run.
        await mgr.disconnect_user_session(UPSTREAM_ID, ALICE)
        fresh.append(await acquire(mgr, upstream, store))
        await heal_stalled_session(**kwargs)

    monkeypatch.setattr(
        tool_router_module, "heal_stalled_session",
        heal_after_someone_else_replaced,
    )
    try:
        assert not (await call_echo(router, "warm")).isError
        await stop_upstream(server, server_task)
        await asyncio.sleep(0.5)  # let the cached session notice the drop
        server, server_task = await restart_upstream_on(port, gate)

        result = await asyncio.wait_for(call_echo(router, "after"), timeout=60)
        assert not result.isError, result
        assert len(fresh) == 1, "precondition: the rival reconnect happened"
        current = mgr.get_session(UPSTREAM_ID, user_id=ALICE)
        assert current is fresh[0], (
            "the late heal evicted the fresh session another request had "
            "just built"
        )
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


async def restart_upstream_on(
    port: int, gate: ConnectionGate,
) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    app = make_upstream_server(gate).streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    task = asyncio.create_task(server.serve())
    await wait_until_serving(port)
    return server, task


class _DeadSession:
    """The session a liveness probe found dead. Its failing call is the
    moment another request replaces it with a fresh session."""

    def __init__(self, on_probe: Callable[[], Any]) -> None:
        self._on_probe = on_probe

    async def list_tools(self) -> None:
        await self._on_probe()
        raise RuntimeError("session is dead")


@pytest.mark.asyncio
async def test_the_liveness_probe_only_evicts_the_session_it_probed(
    tmp_path: Path,
) -> None:
    """The probe snapshots a session, probes it, and tears it down if the
    probe fails. By then another request may have replaced it. The probe
    must tear down only the session it probed, never the replacement."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    fresh: list[ClientSession] = []

    async def someone_else_reconnects() -> None:
        await mgr.disconnect_user_session(UPSTREAM_ID, ALICE)
        fresh.append(await acquire(mgr, upstream, store))

    dead = _DeadSession(someone_else_reconnects)
    seed_user_session(mgr, UPSTREAM_ID, ALICE, session=dead)
    try:
        await probe_upstream_liveness(
            DEFAULT_ORG_ID, upstream, ALICE, dead,  # type: ignore[arg-type]
            mgr, store, GATEWAY_URL,
        )
        assert len(fresh) == 1
        current = mgr.get_session(UPSTREAM_ID, user_id=ALICE)
        assert current is fresh[0], (
            "the probe tore down the replacement instead of the dead session "
            "it probed"
        )
        assert await whoami(current) == "Bearer token-1"
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


class _SlowToCloseTask:
    """The connection behind an idle session. Closing it takes a moment,
    and during that moment another user's session gets replaced."""

    def __init__(self, during_close: Callable[[], Any]) -> None:
        self._during_close = during_close
        self.server_info = None
        self.self_description = None

    def is_transport_alive(self) -> bool:
        return True

    async def close(self) -> None:
        await self._during_close()


@pytest.mark.asyncio
async def test_the_idle_sweep_skips_a_session_replaced_while_it_swept(
    tmp_path: Path,
) -> None:
    """The sweep lists idle sessions, then closes them one by one. While it
    closes the first, the second user's idle session is replaced by a
    fresh one. The sweep must skip the fresh session: it is neither the
    session it listed nor idle."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    fresh: list[ClientSession] = []

    async def bob_reconnects() -> None:
        await mgr.disconnect_user_session(UPSTREAM_ID, BOB)
        fresh.append(await acquire(mgr, upstream, store, user=BOB))

    seed_user_session(
        mgr, UPSTREAM_ID, ALICE, task=_SlowToCloseTask(bob_reconnects),  # type: ignore[arg-type]
    )
    seed_user_session(mgr, UPSTREAM_ID, BOB)
    idle_since = -USER_SESSION_IDLE_TIMEOUT - 60.0
    mgr._user_session_last_used[(ALICE, UPSTREAM_ID)] = idle_since
    mgr._user_session_last_used[(BOB, UPSTREAM_ID)] = idle_since
    try:
        await mgr._sweep_idle_sessions()
        assert len(fresh) == 1, "precondition: Bob reconnected mid-sweep"
        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
        assert mgr.has_user_session(UPSTREAM_ID, BOB), (
            "the sweep closed Bob's fresh session because his OLD one had "
            "been idle"
        )
        assert mgr.get_session(UPSTREAM_ID, user_id=BOB) is fresh[0]
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_the_idle_sweep_skips_a_session_used_while_it_swept(
    tmp_path: Path,
) -> None:
    """Same sweep, but Bob's listed session is not replaced: he USES it
    while the sweep closes Alice's. It is no longer idle, so the sweep
    must leave it alone."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    used: list[ClientSession] = []

    async def bob_uses_his_session() -> None:
        used.append(await acquire(mgr, upstream, store, user=BOB))

    # Alice first, so the sweep lists (and closes) her before Bob.
    seed_user_session(
        mgr, UPSTREAM_ID, ALICE, task=_SlowToCloseTask(bob_uses_his_session),  # type: ignore[arg-type]
    )
    bob_session = await acquire(mgr, upstream, store, user=BOB)
    idle_since = -USER_SESSION_IDLE_TIMEOUT - 60.0
    mgr._user_session_last_used[(ALICE, UPSTREAM_ID)] = idle_since
    mgr._user_session_last_used[(BOB, UPSTREAM_ID)] = idle_since
    try:
        await mgr._sweep_idle_sessions()
        assert used == [bob_session], "precondition: Bob used his session"
        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
        assert mgr.get_session(UPSTREAM_ID, user_id=BOB) is bob_session, (
            "the sweep closed a session that was used after it was listed"
        )
        assert await whoami(bob_session) == "Bearer token-b"
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


# --- Nothing on the OAuth path leaks a raw KeyError --------------------------


@pytest.mark.asyncio
async def test_no_sign_in_surfaces_as_session_unavailable(
    tmp_path: Path,
) -> None:
    """With no stored sign-in there is no session to get. That must come
    back as ``SessionUnavailable`` (the router turns it into "please sign
    in"), not as a lookup error."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {})
    mgr = UpstreamClientManager([upstream])
    try:
        with pytest.raises(SessionUnavailable):
            await acquire(mgr, upstream, store)
        assert gate.opened == 0
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


# --- Requests that arrive during a deliberate re-sign-in --------------------


@pytest.mark.asyncio
async def test_a_request_during_a_re_sign_in_gets_the_new_session(
    tmp_path: Path,
) -> None:
    """While a re-sign-in waits out a reconnect that started from the OLD
    token, a new request arrives. It must end on the re-sign-in's new
    session. Joining the old reconnect would hand it a session the
    re-sign-in closes a moment later: the MCPOLIS-BACKEND-W symptom again,
    inside the re-sign-in window. (Found by the second review.)"""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        in_flight = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)
        await store.put_user_token(
            DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, make_token("token-2"),
        )
        re_sign_in = asyncio.create_task(try_connect_with_stored_tokens(
            DEFAULT_ORG_ID, upstream, ALICE, store, mgr, GATEWAY_URL,
        ))
        await let_others_run()
        late = asyncio.create_task(acquire(mgr, upstream, store))
        await let_others_run()
        gate.release.set()
        _, outcome, late_session = await asyncio.wait_for(
            asyncio.gather(in_flight, re_sign_in, late), timeout=30,
        )
        assert outcome is not None and outcome.connected
        assert await whoami(late_session) == "Bearer token-2", (
            "a request that arrived after the re-sign-in began must get the "
            "re-sign-in's session, not one the re-sign-in closed"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


def make_first_connect_fail(
    mgr: UpstreamClientManager, release: asyncio.Event, message: str,
) -> Callable[..., Any]:
    """Fault injection for one connect: the FIRST connection the manager
    opens waits for ``release`` and then fails with ``message``; every
    later one is real."""
    real = mgr._create_task  # pyright: ignore[reportPrivateUsage]
    calls = 0

    async def create_task(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
            raise RuntimeError(message)
        return await real(*args, **kwargs)

    return create_task


@pytest.mark.asyncio
async def test_a_tool_call_during_a_failing_re_sign_in_reconnects_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard's Connect runs a re-sign-in whose connect fails, while
    the user's MCP client makes a tool call. The tool call must not receive
    the sign-in's raw error: it would escape the router, and the MCP SDK
    would send its text (here an internal address) to the client. It waits
    for the sign-in, then reconnects on its own. (Second review, finding 1.)"""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-2"})
    mgr = UpstreamClientManager([upstream])
    router = make_router(tmp_path, mgr, upstream, store)
    release = asyncio.Event()
    secret = "connect refused by https://10.0.0.5:8443/internal"
    monkeypatch.setattr(
        mgr, "_create_task", make_first_connect_fail(mgr, release, secret),
    )
    try:
        connect = asyncio.create_task(try_connect_with_stored_tokens(
            DEFAULT_ORG_ID, upstream, ALICE, store, mgr, GATEWAY_URL,
        ))
        await let_others_run()
        call = asyncio.create_task(call_echo(router, "hi"))
        await let_others_run()
        release.set()
        outcome, result = await asyncio.wait_for(
            asyncio.gather(connect, call, return_exceptions=True), timeout=30,
        )
        assert outcome is None, "precondition: the re-sign-in's connect failed"
        assert isinstance(result, mcp_types.CallToolResult), (
            f"route_call must return a tool result, it raised {result!r}"
        )
        block = result.content[0]
        assert isinstance(block, mcp_types.TextContent)
        assert secret not in block.text
        assert not result.isError, result
        assert block.text == "echo:hi"
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_reconnect_aborted_by_shutdown_is_session_unavailable(
    tmp_path: Path,
) -> None:
    """A shutdown aborts the reconnect a request waits on. The request must
    get ``SessionUnavailable`` (the router turns it into a tool result),
    not the internal abort."""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        with structlog.testing.capture_logs() as logs:
            waiting = asyncio.create_task(acquire(mgr, upstream, store))
            await wait_until(lambda: gate.opened >= 2)
            await mgr.stop_all()
            with pytest.raises(SessionUnavailable):
                await asyncio.wait_for(waiting, timeout=10)
        assert not events(logs, "upstream.acquire.reconnect_failed"), (
            "a shutdown's abort is expected; it must not be logged as an "
            "unexpected reconnect failure (ERROR, a Sentry alert)"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


class _DeadTransportTask:
    """The connection behind a per-user session whose transport died."""

    server_info = None
    self_description = None
    closed = False

    def is_transport_alive(self) -> bool:
        return False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_a_session_whose_transport_died_is_reconnected(
    tmp_path: Path,
) -> None:
    """A cached per-user session whose transport is known dead is not
    handed out: the request reconnects instead of failing on the zombie."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    dead_task = _DeadTransportTask()
    zombie, _ = seed_user_session(
        mgr, UPSTREAM_ID, ALICE, task=dead_task,  # type: ignore[arg-type]
    )
    try:
        session = await acquire(mgr, upstream, store)
        assert session is not zombie
        assert dead_task.closed, "the zombie's connection must be closed"
        assert await whoami(session) == "Bearer token-1"
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_an_unexpected_reconnect_error_comes_back_as_a_tool_result(
    tmp_path: Path,
) -> None:
    """A failure the reconnect does not classify (here the token store
    breaks mid-reconnect) must come back as a tool result. Raised raw, it
    would escape the router and the MCP SDK would send its text, internal
    addresses included, to the client."""
    gate = ConnectionGate()
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    mgr = UpstreamClientManager([upstream])
    secret = "store unreachable at mongodb://10.0.0.9:27017"

    class BreaksMidReconnect(FileConnectionStore):
        """The token read succeeds; the next read (the reconnect's own
        expiry check, which nothing classifies) fails."""

        reads = 0

        async def get_user_token(  # type: ignore[override]
            self, org_id: str, user_id: str, upstream_id: str,
        ) -> StoredToken | None:
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError(secret)
            return await super().get_user_token(org_id, user_id, upstream_id)

    store = BreaksMidReconnect(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, make_token("token-1"),
    )
    router = make_router(tmp_path, mgr, upstream, store)
    try:
        result = await asyncio.wait_for(call_echo(router, "hi"), timeout=30)
        assert store.reads >= 2, "precondition: the failing read happened"
        assert isinstance(result, mcp_types.CallToolResult)
        assert result.isError
        block = result.content[0]
        assert isinstance(block, mcp_types.TextContent)
        assert secret not in block.text
        assert gate.opened == 0
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_no_session_lands_after_shutdown(tmp_path: Path) -> None:
    """Org deletion tears the manager down while a re-sign-in waits out a
    reconnect. The teardown aborts the reconnect; the re-sign-in must not
    then connect on its own and land a session in the dead manager.
    (Second review, pass 2.)"""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        in_flight = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)
        await store.put_user_token(
            DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, make_token("token-2"),
        )
        re_sign_in = asyncio.create_task(try_connect_with_stored_tokens(
            DEFAULT_ORG_ID, upstream, ALICE, store, mgr, GATEWAY_URL,
        ))
        await let_others_run()
        await mgr.stop_all()
        gate.release.set()
        await asyncio.wait_for(
            asyncio.gather(in_flight, re_sign_in, return_exceptions=True),
            timeout=30,
        )
        await asyncio.sleep(0.5)  # room for a connect started late to land
        assert not mgr.has_user_session(UPSTREAM_ID, ALICE), (
            "a re-sign-in connected after the teardown"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)
