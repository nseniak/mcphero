"""Signing a user out of an upstream (a member's own sign-out on My
Tools, an admin's Sign out on the admin tab) deletes that user's saved
sign-in and closes that user's live session, including a connect still
running for them. Every other user keeps theirs.

Same harness as ``test_user_disconnect_race.py``: a REAL streamable-HTTP
MCP server on loopback, a REAL file-backed token store, the REAL
reconnect path.
"""
import asyncio
from pathlib import Path

import pytest

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.upstream_connection_service import (
    SessionUnavailable,
    sign_out_of_upstream,
)
from tests.unit._user_session_harness import (
    ALICE,
    BOB,
    UPSTREAM_ID,
    ConnectionGate,
    acquire,
    make_store,
    make_upstream,
    start_upstream,
    stop_upstream,
    wait_until,
)


@pytest.mark.asyncio
async def test_sign_out_closes_only_that_users_session(tmp_path: Path) -> None:
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a", BOB: "token-b"})
    mgr = UpstreamClientManager([upstream])
    try:
        await acquire(mgr, upstream, store, ALICE)
        await acquire(mgr, upstream, store, BOB)

        await sign_out_of_upstream(
            org_id=DEFAULT_ORG_ID,
            upstream_id=UPSTREAM_ID,
            user_id=ALICE,
            connection_store=store,
            client_manager=mgr,
        )

        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is None
        assert mgr.has_user_session(UPSTREAM_ID, BOB), (
            "signing alice out closed bob's session"
        )
        assert await store.get_user_token(DEFAULT_ORG_ID, BOB, UPSTREAM_ID) is not None
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_sign_out_ends_a_connect_still_running_for_that_user(
    tmp_path: Path,
) -> None:
    """Alice's call is reconnecting her from her saved sign-in when she is
    signed out. The connect read the sign-in before it was deleted, so
    unless it is stopped it lands a session after the sign-out."""
    gate = ConnectionGate(hold={2})  # hold the connect, after its probe
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {ALICE: "token-a"})
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store, ALICE))
        await wait_until(lambda: gate.opened >= 2)

        await sign_out_of_upstream(
            org_id=DEFAULT_ORG_ID,
            upstream_id=UPSTREAM_ID,
            user_id=ALICE,
            connection_store=store,
            client_manager=mgr,
        )
        gate.release.set()
        outcome = await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )
        await asyncio.sleep(0.5)  # room for a connect that was not stopped

        assert not mgr.has_user_session(UPSTREAM_ID, ALICE), (
            "a session landed after the sign-out"
        )
        assert isinstance(outcome[0], SessionUnavailable), outcome
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)
