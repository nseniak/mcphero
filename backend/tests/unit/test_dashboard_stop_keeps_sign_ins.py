"""The dashboard's Stop (Disconnect) keeps every saved sign-in, and the
dashboard shows the stopped server as one Start brings back without a
sign-in. Start by any admin is not a take-over: it is never refused
because another admin's sign-in is the one kept.

The session side (every live session closed, calls refused until Start)
is in ``test_stop_closes_every_session.py``.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from tests.unit._dev_stub_login import accept_invitation, login_as
from tests.unit.test_dashboard_api import make_test_client

ALICE = "alice@example.com"
DEV = "dev@example.com"


def make_token(access_token: str) -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=[],
        updated_at=datetime.now(UTC),
    )


def make_client_with_sign_ins(tmp_path: Path) -> tuple[TestClient, FileConnectionStore]:
    """Logged in as admin@example.com. Alice (a second admin, who accepted
    her invitation) holds the saved admin sign-in for ``mixpanel``; dev@
    (a member) has their own."""
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/users", json={"email": ALICE, "role": "admin"})
    assert resp.status_code == 201, resp.text
    accept_invitation(client, ALICE)
    login_as(client, "admin@example.com")
    store = FileConnectionStore(tmp_path / "data")

    async def seed() -> None:
        await store.put_user_token("default", ALICE, "mixpanel", make_token("a"))
        await store.put_user_token("default", DEV, "mixpanel", make_token("d"))
    asyncio.run(seed())
    return client, store


def saved_sign_in(store: FileConnectionStore, user: str) -> OAuthToken | None:
    return asyncio.run(store.get_user_token("default", user, "mixpanel"))


def test_stop_keeps_the_admin_and_member_sign_ins(tmp_path: Path) -> None:
    client, store = make_client_with_sign_ins(tmp_path)

    resp = client.post("/api/admin/upstreams/mixpanel/disconnect")

    assert resp.status_code == 200, resp.text
    assert saved_sign_in(store, ALICE) is not None, (
        "Stop deleted the admin's saved sign-in"
    )
    assert saved_sign_in(store, DEV) is not None, (
        "Stop deleted a member's saved sign-in"
    )


def test_a_stopped_server_shows_as_stopped_with_its_sign_in_kept(
    tmp_path: Path,
) -> None:
    client, _ = make_client_with_sign_ins(tmp_path)
    before = client.get("/api/admin/upstreams/mixpanel").json()
    assert before["ready"] is True
    assert before["stopped"] is False

    client.post("/api/admin/upstreams/mixpanel/disconnect")

    detail = client.get("/api/admin/upstreams/mixpanel").json()
    assert detail["ready"] is False, "a stopped server must not show Ready"
    assert detail["stopped"] is True
    assert detail["slot_owner"] == ALICE, "the kept sign-in is shown"
    assert detail["disconnect_reason"] is None
    listed = {u["id"]: u for u in client.get("/api/admin/upstreams").json()}
    assert listed["mixpanel"]["ready"] is False
    assert listed["mixpanel"]["stopped"] is True
    assert listed["mixpanel"]["slot_owner"] == ALICE


def test_start_after_stop_is_not_a_take_over(tmp_path: Path) -> None:
    """admin@ stops the server alice signed in to, then starts it again.
    Alice's sign-in is kept, so this Start must not be refused with
    "already connected, disconnect first" (Disconnect no longer signs
    anyone out, so that advice would lead nowhere)."""
    client, store = make_client_with_sign_ins(tmp_path)
    client.post("/api/admin/upstreams/mixpanel/disconnect")

    resp = client.post("/api/admin/upstreams/mixpanel/connect")

    assert resp.status_code == 200, resp.text
    assert resp.json().get("authorization_url") is None, (
        "Start asked for a sign-in although one was kept"
    )
    detail = client.get("/api/admin/upstreams/mixpanel").json()
    assert detail["stopped"] is False, "Start left the server stopped"
    assert asyncio.run(store.is_enabled("default", "mixpanel"))
    assert saved_sign_in(store, ALICE) is not None


def test_a_stopped_server_without_a_kept_sign_in_still_shows_stopped(
    tmp_path: Path,
) -> None:
    """Stopped with no admin sign-in kept: the dashboard still shows it
    stopped (Start will need a sign-in, so it offers Authenticate)."""
    client = make_test_client(tmp_path)
    client.post("/api/admin/upstreams/mixpanel/disconnect")

    detail = client.get("/api/admin/upstreams/mixpanel").json()

    assert detail["ready"] is False
    assert detail["stopped"] is True
    assert detail["slot_owner"] is None


def test_a_personal_sign_in_on_a_stopped_server_is_refused(
    tmp_path: Path,
) -> None:
    """A personal sign-in (the My Tools door) on a stopped server is
    refused before any sign-in page opens: the session it leads to would
    be refused anyway."""
    client, _ = make_client_with_sign_ins(tmp_path)
    client.post("/api/admin/upstreams/mixpanel/disconnect")

    resp = client.get("/api/auth/connect/mixpanel")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("authorization_url") is None
    assert "stopped" in (body.get("error") or ""), body
