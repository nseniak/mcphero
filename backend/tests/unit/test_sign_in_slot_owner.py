"""One rule decides who holds an upstream's admin sign-in slot.

With ``per_user_oauth`` every admin who uses the MCP has a sign-in row,
so several admins can hold one. The dashboard ("Ready, by X"), Remove
sign-in, Connect's "already signed in" refusal and the gateway's
``admin_oauth`` routing used three different rules and disagreed: the
refusal named an admin the dashboard didn't show, and removing the
shown admin's sign-in still left Connect refused by someone else. They
all use ``slot_owner_of`` now (the admin who signed in last), so what
the dashboard shows is what refuses you, and removing it moves the slot
to the next admin the dashboard then shows.

A token refresh does not move the slot (it used to: every refresh is a
save, and the rule was the last save), nor does the deploy that added
sign-in times: a row saved before it counts its last save. And on an
``admin_oauth`` MCP, a sign-in that another admin beat to the slot while
it was on the consent page is refused at its callback, as Connect would
have refused it; on a ``per_user_oauth`` MCP each admin's sign-in is
their own and lands.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthToken as SdkToken

from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
from mcpolis.adapters.auth.pending_auth import PendingAuth, PendingAuthCoordinator
from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.services import upstream_connection_service as ucs
from mcpolis.domain.services.upstream_connection_service import (
    initiate_oauth_connection,
    slot_owner_of,
)
from tests.unit._dev_stub_login import accept_invitation, login_as
from tests.unit._fake_oauth_upstream import (
    FakeOAuthUpstream,
    make_oauth_protected_upstream,
    serve_in_thread,
    start_fake_oauth_upstream,
)
from tests.unit._user_session_harness import stop_upstream
from tests.unit.test_dashboard_api import make_test_client

ADMIN = "admin@example.com"
ALICE = "alice@example.com"
CAROL = "carol@example.com"


def make_token(access_token: str) -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=[],
    )


def make_client_with_two_admin_sign_ins(
    tmp_path: Path,
) -> tuple[TestClient, FileConnectionStore]:
    """Logged in as admin@. alice@ and carol@ are admins too (they
    accepted their invitations); alice signed in to ``mixpanel`` first,
    carol later, so carol's is the newest admin sign-in."""
    client = make_test_client(tmp_path)
    for email in (ALICE, CAROL):
        resp = client.post("/api/admin/users", json={"email": email, "role": "admin"})
        assert resp.status_code == 201, resp.text
        accept_invitation(client, email)
        login_as(client, ADMIN)
    store = FileConnectionStore(tmp_path / "data")

    async def seed() -> None:
        await store.put_user_token("default", ALICE, "mixpanel", make_token("a"))
        await asyncio.sleep(0.01)
        await store.put_user_token("default", CAROL, "mixpanel", make_token("c"))
    asyncio.run(seed())
    return client, store


def shown_owner(client: TestClient) -> str | None:
    return client.get("/api/admin/upstreams/mixpanel").json()["slot_owner"]


def test_the_connect_refusal_names_the_admin_the_dashboard_shows(
    tmp_path: Path,
) -> None:
    client, _ = make_client_with_two_admin_sign_ins(tmp_path)
    assert shown_owner(client) == CAROL

    refused = client.post("/api/admin/upstreams/mixpanel/connect")

    assert refused.status_code == 409, refused.text
    assert CAROL in refused.text


def test_removing_the_shown_sign_in_moves_the_slot_to_the_next_shown_admin(
    tmp_path: Path,
) -> None:
    client, _ = make_client_with_two_admin_sign_ins(tmp_path)

    removed = client.post(
        "/api/admin/upstreams/mixpanel/sign-out", json={"email": CAROL},
    )
    assert removed.status_code == 200, removed.text

    assert shown_owner(client) == ALICE
    refused = client.post("/api/admin/upstreams/mixpanel/connect")
    assert refused.status_code == 409, refused.text
    assert ALICE in refused.text

    client.post("/api/admin/upstreams/mixpanel/sign-out", json={"email": ALICE})

    assert shown_owner(client) is None
    resp = client.post("/api/admin/upstreams/mixpanel/connect")
    assert resp.status_code != 409, resp.text
    assert "already signed in" not in resp.text


def test_an_admin_holding_the_shown_sign_in_may_sign_in_again(
    tmp_path: Path,
) -> None:
    """Carol's own Connect isn't refused because alice also has a row:
    the slot is carol's."""
    client, _ = make_client_with_two_admin_sign_ins(tmp_path)
    login_as(client, CAROL)

    resp = client.post("/api/admin/upstreams/mixpanel/connect")

    assert resp.status_code != 409, resp.text


# --- A refresh is not a sign-in ---


async def refresh_like_the_periodic_refresh(
    storage: McpTokenStorage, new_access_token: str,
) -> None:
    """What a token refresh does to the stored row: the sign-in library
    loads it, the upstream issues new tokens, the library saves them."""
    await storage.get_tokens()
    await storage.set_tokens(SdkToken(
        access_token=new_access_token, token_type="Bearer", expires_in=3600,
    ))


def test_a_token_refresh_does_not_move_the_admin_slot(tmp_path: Path) -> None:
    """Refreshing alice's token moved the slot from carol to alice: the
    admin tab's owner, Connect's refusal and, for ``admin_oauth``, the
    upstream account every tool call runs as."""
    client, store = make_client_with_two_admin_sign_ins(tmp_path)
    assert shown_owner(client) == CAROL

    async def refresh_alice() -> str | None:
        await asyncio.sleep(0.01)
        await refresh_like_the_periodic_refresh(
            McpTokenStorage(store, "default", "mixpanel", ALICE), "a-rotated",
        )
        # The rule an admin_oauth upstream's tool calls use.
        return await slot_owner_of(
            store, "default", "mixpanel", admin_emails=[ALICE, CAROL],
        )

    routed_to = asyncio.run(refresh_alice())

    assert shown_owner(client) == CAROL
    assert routed_to == CAROL
    refused = client.post("/api/admin/upstreams/mixpanel/connect")
    assert refused.status_code == 409, refused.text
    assert CAROL in refused.text


# --- Sign-ins saved before sign-in times existed ---


def seed_sign_ins_saved_before_sign_in_times(
    tmp_path: Path, saved_ago: dict[str, timedelta],
) -> None:
    """Make each admin's ``mixpanel`` sign-in look as the release before
    sign-in times saved it: no ``signed_in_at``, last saved
    ``saved_ago[email]`` ago (a sign-in, or a refresh of it). Every row
    looked like this at the deploy that added sign-in times."""
    path = tmp_path / "data" / "connections.json"
    rows = json.loads(path.read_text())
    for email, ago in saved_ago.items():
        row = rows[f"user:mixpanel:{email}"]
        del row["signed_in_at"]
        row["updated_at"] = (datetime.now(UTC) - ago).isoformat()
    path.write_text(json.dumps(rows))


def test_the_deploy_that_added_sign_in_times_leaves_the_slot_with_its_admin(
    tmp_path: Path,
) -> None:
    """Before sign-in times, the slot went to the admin whose row was
    saved last: carol here. At the deploy no row had a sign-in time, they
    all tied, and the slot (with, for ``admin_oauth``, every member's tool
    calls) moved to the first admin in alphabetical order, alice, whose
    sign-in may be stale."""
    client, store = make_client_with_two_admin_sign_ins(tmp_path)
    seed_sign_ins_saved_before_sign_in_times(
        tmp_path, {ALICE: timedelta(days=20), CAROL: timedelta(minutes=5)},
    )

    routed_to = asyncio.run(slot_owner_of(
        store, "default", "mixpanel", admin_emails=[ALICE, CAROL],
    ))

    assert shown_owner(client) == CAROL
    assert routed_to == CAROL


def test_a_refresh_of_a_row_saved_before_sign_in_times_does_not_move_the_slot(
    tmp_path: Path,
) -> None:
    """Such a row's first refresh keeps its last save as its sign-in time.
    Taking the refresh's own time would hand the slot to alice, whose
    token was refreshed last: the instability sign-in times ended."""
    client, store = make_client_with_two_admin_sign_ins(tmp_path)
    seed_sign_ins_saved_before_sign_in_times(
        tmp_path, {ALICE: timedelta(days=20), CAROL: timedelta(minutes=5)},
    )

    async def refresh_alice() -> str | None:
        await refresh_like_the_periodic_refresh(
            McpTokenStorage(store, "default", "mixpanel", ALICE), "a-rotated",
        )
        return await slot_owner_of(
            store, "default", "mixpanel", admin_emails=[ALICE, CAROL],
        )

    routed_to = asyncio.run(refresh_alice())

    refreshed = asyncio.run(store.get_user_token("default", ALICE, "mixpanel"))
    assert refreshed is not None and refreshed.access_token == "a-rotated"
    assert shown_owner(client) == CAROL
    assert routed_to == CAROL


# --- Another admin takes the slot during a sign-in ---


def mcp_servers_with_mixpanel_at(base: str) -> str:
    return json.dumps({
        "mcpServers": {
            "github": {"url": "http://localhost:9000/mcp"},
            "mixpanel": {"url": f"{base}/mcp"},
        },
    })


def signed_state_of(sign_in_link: str) -> str:
    return parse_qs(urlparse(sign_in_link).query)["state"][0]


def coordinator_of(client: TestClient) -> PendingAuthCoordinator:
    return client.app.state.auth_coordinator  # type: ignore[attr-defined,no-any-return]


def connect_while_carol_signs_in(
    tmp_path: Path, auth_mode: AuthMode,
) -> tuple[TestClient, FileConnectionStore, PendingAuth, str]:
    """admin@ clicks Connect on mixpanel (an ``auth_mode`` MCP) while no
    admin holds its sign-in slot, carol signs in while admin@ is on the
    consent page, then the upstream's callback brings admin@'s code.
    Returns the client, the store, admin@'s sign-in flow and the page the
    callback answered."""
    with serve_in_thread(FakeOAuthUpstream()) as base:
        client = make_test_client(
            tmp_path, mcp_servers=mcp_servers_with_mixpanel_at(base),
        )
        switched = client.put(
            "/api/admin/upstreams/mixpanel", json={"auth_mode": auth_mode.value},
        )
        assert switched.status_code == 200, switched.text
        invited = client.post("/api/admin/users", json={"email": CAROL, "role": "admin"})
        assert invited.status_code == 201, invited.text
        accept_invitation(client, CAROL)
        login_as(client, ADMIN)
        connect = client.post("/api/admin/upstreams/mixpanel/connect")
        assert connect.status_code == 200, connect.text
        link = connect.json()["authorization_url"]
        assert link, connect.text
        flow = coordinator_of(client).get_pending("default", "mixpanel", ADMIN)
        assert flow is not None
        store = FileConnectionStore(tmp_path / "data")
        asyncio.run(store.put_user_token("default", CAROL, "mixpanel", make_token("c")))

        callback = client.get(
            "/api/oauth/upstream/callback",
            params={"code": "the-code", "state": signed_state_of(link)},
        )
    return client, store, flow, callback.text


def test_an_admin_oauth_sign_in_another_admin_beat_to_the_slot_is_refused_at_its_callback(
    tmp_path: Path,
) -> None:
    """On an admin_oauth MCP one admin sign-in serves every member. Connect
    would refuse admin@ now that carol holds it; the callback does too,
    naming carol, instead of landing a second admin sign-in."""
    client, store, _flow, page = connect_while_carol_signs_in(
        tmp_path, AuthMode.admin_oauth,
    )

    assert "Authorization failed" in page
    assert CAROL in page
    assert coordinator_of(client).get_pending("default", "mixpanel", ADMIN) is None
    assert asyncio.run(store.get_user_token("default", ADMIN, "mixpanel")) is None


def test_a_per_user_oauth_admins_own_sign_in_lands_whoever_signed_in_meanwhile(
    tmp_path: Path,
) -> None:
    """On a per_user_oauth MCP each admin's sign-in is their own: their
    tool calls run as them. carol signing in meanwhile refuses nothing,
    neither at the callback nor right before the save (the check the
    sign-in's storage asks then)."""
    _client, _store, flow, page = connect_while_carol_signs_in(
        tmp_path, AuthMode.per_user_oauth,
    )

    assert "Authorization successful" in page
    assert flow.auth_code == "the-code"
    assert flow.refusal is None
    assert asyncio.run(flow.check_sign_in()) is None


async def test_a_sign_in_refused_right_before_its_save_saves_nothing_and_says_why(
    tmp_path: Path,
) -> None:
    """The slot can be taken after the callback too, while the code is
    exchanged: the sign-in's check runs again right before its tokens are
    saved, and the person is told why it did not land."""
    fake = FakeOAuthUpstream(hold_token_exchange=True)
    server, task = await start_fake_oauth_upstream(fake)
    store = FileConnectionStore(tmp_path)
    coordinator = PendingAuthCoordinator(b"k" * 32)
    upstream = make_oauth_protected_upstream(fake.base)
    manager = UpstreamClientManager([upstream])
    taken_by: list[str] = []
    errors: list[str] = []

    async def slot_check() -> str | None:
        if not taken_by:
            return None
        return f"'{taken_by[0]}' is already signed in to this MCP."

    try:
        result = await initiate_oauth_connection(
            "default", upstream, ADMIN, store, coordinator, manager,
            "http://localhost:8000",
            on_error=lambda message, _reason: errors.append(message),
            sign_in_check=slot_check,
        )
        assert result.authorization_url, result.error
        pending = coordinator.get_pending("default", upstream.id, ADMIN)
        assert pending is not None
        assert coordinator.complete_by_key(
            "default", upstream.id, ADMIN, "the-code", pending.auth_state,
        ) is pending
        assert await asyncio.to_thread(fake.token_requested.wait, 10)

        taken_by.append(CAROL)  # carol signs in during the code exchange
        fake.release_token.set()
        await ucs._sign_in_waits.drain(10)  # pyright: ignore[reportPrivateUsage]

        assert await store.get_user_token("default", ADMIN, upstream.id) is None
        assert errors == [f"'{CAROL}' is already signed in to this MCP."]
    finally:
        await manager.disconnect_all_user_sessions(ADMIN)
        await stop_upstream(server, task)
