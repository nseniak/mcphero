"""A deliberate teardown (a user's Disconnect, an admin's Disconnect, a
user removed from the org) must also stop a reconnect that is still
running for that user. Otherwise the reconnect lands a session right after
the teardown, and the user is connected again although nobody signed in.

Same harness as ``test_user_session_race.py``: a REAL streamable-HTTP MCP
server on loopback whose per-connection startup hook holds a chosen
connect in flight, a REAL file-backed token store, the REAL reconnect path.
"""
import asyncio
from pathlib import Path

import pytest

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services import upstream_connection_service
from mcpolis.domain.services.upstream_connection_service import (
    SessionUnavailable,
    heal_stalled_session,
)
from tests.unit._shared_session_harness import lose_cancels_while_connecting
from tests.unit._user_session_harness import (
    ALICE,
    UPSTREAM_ID,
    ConnectionGate,
    acquire,
    make_store,
    make_upstream,
    start_upstream,
    stop_upstream,
    wait_until,
)


async def room_to_land() -> None:
    """Time for a connect that was not stopped to install its session."""
    await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_a_disconnect_stops_a_reconnect_that_is_still_running(
    tmp_path: Path,
) -> None:
    """A tool call is reconnecting Alice from her stored sign-in when she
    clicks Disconnect. The route deletes her sign-in, then her session.
    The reconnect had already read the sign-in, so unless the Disconnect
    stops it, it lands a session after the Disconnect."""
    gate = ConnectionGate(hold={2})  # hold the connect, after its probe
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)

        # The Disconnect route, in its own order.
        await store.delete_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        await mgr.disconnect_user_session(UPSTREAM_ID, ALICE)
        gate.release.set()
        outcome = await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )
        await room_to_land()

        assert not mgr.has_user_session(UPSTREAM_ID, ALICE), (
            "a session landed after Disconnect"
        )
        assert isinstance(outcome[0], SessionUnavailable), (
            f"the interrupted tool call must fail cleanly; got {outcome[0]!r}"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_removing_a_user_stops_their_reconnect_that_is_still_running(
    tmp_path: Path,
) -> None:
    """Alice is removed from the org while a tool call of hers is
    reconnecting. She has no session yet, only the connect in flight, so a
    teardown that only walks existing sessions misses it."""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)

        await mgr.disconnect_all_user_sessions(ALICE)
        gate.release.set()
        await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )
        await room_to_land()

        assert not mgr.has_user_session(UPSTREAM_ID, ALICE), (
            "a session landed for a user who was removed"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_heal_never_stops_a_reconnect_that_is_running(
    tmp_path: Path,
) -> None:
    """A call on a session that has since been replaced stalls, and its
    heal runs while a reconnect is building the replacement. The heal
    drops only the session it saw stall; the running reconnect is the
    replacement and must not be stopped, unlike on Disconnect."""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)

        stale = object()  # the session the stalled call ran on
        await heal_stalled_session(
            org_id=DEFAULT_ORG_ID,
            upstream=upstream,
            effective_user=ALICE,
            client_manager=mgr,
            stalled_session=stale,  # type: ignore[arg-type]
        )
        gate.release.set()
        session = await asyncio.wait_for(request, timeout=30)

        assert mgr.find_user_session(UPSTREAM_ID, ALICE) is session, (
            "the heal stopped the reconnect that was replacing the session"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_removing_a_server_stops_and_drops_its_users_sessions(
    tmp_path: Path,
) -> None:
    """An admin removes the MCP server while Alice's reconnect to it is
    running. Nothing of hers may survive it: added again under the same
    id, the server would otherwise hand her the old session, built from
    the old configuration."""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)

        await mgr.unregister_upstream(UPSTREAM_ID)
        gate.release.set()
        await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )
        await room_to_land()
        mgr.register_upstream(upstream)  # added again, same id

        assert mgr.find_user_session(UPSTREAM_ID, ALICE) is None, (
            "a session from before the removal survived it"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_reconnect_whose_cancel_was_lost_still_respects_the_disconnect(
    tmp_path: Path,
) -> None:
    """Even if the Disconnect's cancel never reaches the reconnect, the
    reconnect must not record its session afterwards: it checks, once
    connected, whether its slot was aborted meanwhile. (Review pass 2,
    N1, second layer.)"""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    lose_cancels_while_connecting(mgr)
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)

        await store.delete_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        disconnect = asyncio.create_task(
            mgr.disconnect_user_session(UPSTREAM_ID, ALICE),
        )
        await asyncio.sleep(0.05)
        gate.release.set()
        await asyncio.wait_for(disconnect, timeout=30)
        await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )
        await room_to_land()

        assert not mgr.has_user_session(UPSTREAM_ID, ALICE), (
            "a session landed after Disconnect"
        )
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_removal_that_ends_a_reconnect_is_not_a_refresh_failure(
    tmp_path: Path,
) -> None:
    """A user is removed while a reconnect of theirs runs (their sign-in
    is deleted only after the teardown). Ending that reconnect is not a
    failed token refresh: nothing is counted against the sign-in, and no
    error banner appears on the server for every admin. (Review pass 3,
    P1.)"""
    gate = ConnectionGate(hold={2})
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    lose_cancels_while_connecting(mgr)
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)

        removal = asyncio.create_task(mgr.disconnect_all_user_sessions(ALICE))
        await asyncio.sleep(0.05)
        gate.release.set()
        await asyncio.wait_for(removal, timeout=30)
        outcome = await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )

        assert await store.get_connection_error(DEFAULT_ORG_ID, UPSTREAM_ID) is None
        assert await store.get_refresh_failures(
            DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        ) is None
        assert isinstance(outcome[0], SessionUnavailable), outcome
        assert outcome[0].reason == "connect_aborted", outcome[0].reason
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_disconnect_during_the_token_refresh_is_honoured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Disconnect lands while the reconnect refreshes the token, and
    its cancel is lost there. The reconnect must still discard the session
    it then builds: the abort counts from the moment the reconnect began,
    not from when it started connecting. (Review pass 3, P2.)"""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-1"})
    mgr = UpstreamClientManager([upstream])
    refreshing = asyncio.Event()
    finish_refresh = asyncio.Event()

    async def refresh_losing_cancels(*_args: object, **_kwargs: object) -> None:
        refreshing.set()
        while not finish_refresh.is_set():
            try:
                await finish_refresh.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(
        upstream_connection_service, "_trigger_silent_refresh",
        refresh_losing_cancels,
    )
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await asyncio.wait_for(refreshing.wait(), timeout=10)

        disconnect = asyncio.create_task(
            mgr.disconnect_user_session(UPSTREAM_ID, ALICE),
        )
        await asyncio.sleep(0.05)
        finish_refresh.set()
        await asyncio.wait_for(disconnect, timeout=30)
        await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )
        await room_to_land()

        assert not mgr.has_user_session(UPSTREAM_ID, ALICE), (
            "a session landed after Disconnect"
        )
    finally:
        finish_refresh.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)
