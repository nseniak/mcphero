"""The admin tab's Sign out: any admin deletes the saved admin sign-in an
OAuth upstream shows ("Ready, by alice@"), so another admin can sign in
with Authenticate. That is the take-over, now that Stop (Disconnect)
keeps every sign-in.

Members' own sign-ins are never touched, and a stopped upstream stays
stopped. The live-session side is in ``test_sign_out_of_upstream.py``.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from httpx import Response

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from tests.unit._dev_stub_login import accept_invitation, login_as
from tests.unit.test_dashboard_api import make_test_client

ALICE = "alice@example.com"
ADMIN = "admin@example.com"
DEV = "dev@example.com"


def make_token(access_token: str) -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=[],
    )


def make_client_with_sign_ins(
    tmp_path: Path, signed_in: list[str],
) -> tuple[TestClient, FileConnectionStore]:
    """Logged in as admin@. alice@ is a second admin (she accepted her
    invitation), dev@ a member. Each user in ``signed_in`` holds a saved
    sign-in for ``mixpanel``, saved in list order (the store stamps the
    save time, so the last is newest)."""
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/users", json={"email": ALICE, "role": "admin"})
    assert resp.status_code == 201, resp.text
    accept_invitation(client, ALICE)
    login_as(client, "admin@example.com")
    store = FileConnectionStore(tmp_path / "data")

    async def seed() -> None:
        for user in signed_in:
            await store.put_user_token(
                "default", user, "mixpanel", make_token(user),
            )
    asyncio.run(seed())
    return client, store


def saved_sign_in(store: FileConnectionStore, user: str) -> OAuthToken | None:
    return asyncio.run(store.get_user_token("default", user, "mixpanel"))


def remove_sign_in(client: TestClient, shown: str | None) -> Response:
    """Click Remove sign-in on the row that shows ``shown``'s sign-in."""
    return client.post(
        "/api/admin/upstreams/mixpanel/sign-out", json={"email": shown},
    )


def test_sign_out_deletes_the_shown_admin_sign_in_only(tmp_path: Path) -> None:
    client, store = make_client_with_sign_ins(tmp_path, [ALICE, DEV])
    assert client.get("/api/admin/upstreams/mixpanel").json()["slot_owner"] == ALICE

    resp = remove_sign_in(client, ALICE)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "signed_out", "email": ALICE}
    assert saved_sign_in(store, ALICE) is None
    assert saved_sign_in(store, DEV) is not None, (
        "Sign out deleted a member's own sign-in"
    )
    detail = client.get("/api/admin/upstreams/mixpanel").json()
    assert detail["ready"] is False
    assert detail["slot_owner"] is None


def test_sign_out_picks_the_admin_the_dashboard_shows(tmp_path: Path) -> None:
    """Two admins hold a sign-in. The dashboard shows the most recent one
    (alice, saved last; admin@ comes first in the admin list), so that is
    the one Sign out deletes."""
    client, store = make_client_with_sign_ins(tmp_path, [ADMIN, ALICE])
    shown = client.get("/api/admin/upstreams/mixpanel").json()["slot_owner"]
    assert shown == ALICE

    resp = remove_sign_in(client, shown)

    assert resp.json()["email"] == ALICE
    assert saved_sign_in(store, ALICE) is None
    assert saved_sign_in(store, ADMIN) is not None


def test_after_sign_out_another_admin_can_authenticate(tmp_path: Path) -> None:
    """The take-over: admin@ signs alice out, then signs in in her place,
    with no "already signed in" refusal."""
    client, _ = make_client_with_sign_ins(tmp_path, [ALICE])
    refused = client.post("/api/admin/upstreams/mixpanel/connect")
    assert refused.status_code == 409, refused.text

    remove_sign_in(client, ALICE)
    resp = client.post("/api/admin/upstreams/mixpanel/connect")

    assert resp.status_code == 200, resp.text


def test_sign_out_of_a_stopped_server_keeps_it_stopped(tmp_path: Path) -> None:
    """Stopped with alice's sign-in kept, then signed out: still stopped,
    and Start now needs a sign-in (Authenticate, not Connect)."""
    client, store = make_client_with_sign_ins(tmp_path, [ALICE])
    client.post("/api/admin/upstreams/mixpanel/disconnect")
    stopped = client.get("/api/admin/upstreams/mixpanel").json()
    assert stopped["stopped"] is True
    assert stopped["slot_owner"] == ALICE

    resp = remove_sign_in(client, ALICE)

    assert resp.json()["email"] == ALICE
    assert saved_sign_in(store, ALICE) is None
    detail = client.get("/api/admin/upstreams/mixpanel").json()
    assert detail["ready"] is False
    assert detail["stopped"] is True
    assert detail["slot_owner"] is None
    assert not asyncio.run(store.is_enabled("default", "mixpanel")), (
        "Sign out restarted a stopped server"
    )


def test_sign_out_with_nobody_signed_in_changes_nothing(tmp_path: Path) -> None:
    """Two admins remove alice's sign-in at once: the second finds it
    already gone, and that is not an error."""
    client, store = make_client_with_sign_ins(tmp_path, [ALICE, DEV])
    assert remove_sign_in(client, ALICE).status_code == 200

    resp = remove_sign_in(client, ALICE)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "signed_out", "email": None}
    assert saved_sign_in(store, DEV) is not None


def test_a_second_click_never_removes_the_next_admin(tmp_path: Path) -> None:
    """The dialog named alice. When her sign-in is already gone and the
    dashboard would now show admin@'s, a second click (another tab, or
    another admin) must not delete admin@'s sign-in instead."""
    client, store = make_client_with_sign_ins(tmp_path, [ADMIN, ALICE])
    assert remove_sign_in(client, ALICE).json()["email"] == ALICE
    assert client.get("/api/admin/upstreams/mixpanel").json()["slot_owner"] == ADMIN

    resp = remove_sign_in(client, ALICE)

    assert resp.status_code == 409, resp.text
    assert ADMIN in resp.json()["detail"]
    assert saved_sign_in(store, ADMIN) is not None


def test_remove_sign_in_needs_the_name_the_dialog_showed(tmp_path: Path) -> None:
    client, store = make_client_with_sign_ins(tmp_path, [ALICE])

    resp = client.post("/api/admin/upstreams/mixpanel/sign-out")

    assert resp.status_code == 422, resp.text
    assert saved_sign_in(store, ALICE) is not None


def test_sign_out_refuses_a_server_without_sign_in(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    resp = client.post(
        "/api/admin/upstreams/github/sign-out", json={"email": ADMIN},
    )

    assert resp.status_code == 400, resp.text


def test_sign_out_needs_an_admin(tmp_path: Path) -> None:
    client, store = make_client_with_sign_ins(tmp_path, [ALICE])
    login_as(client, DEV)

    resp = remove_sign_in(client, ALICE)

    assert resp.status_code == 403, resp.text
    assert saved_sign_in(store, ALICE) is not None


def test_sign_out_is_in_the_audit_log(tmp_path: Path) -> None:
    client, _ = make_client_with_sign_ins(tmp_path, [ALICE])

    remove_sign_in(client, ALICE)

    entries = client.get("/api/admin/audit").json()["entries"]
    sign_outs = [e for e in entries if e.get("action") == "sign_out"]
    assert len(sign_outs) == 1, entries
    assert sign_outs[0]["user_id"] == ADMIN
    assert sign_outs[0]["upstream_id"] == "mixpanel"
    assert sign_outs[0]["target_user_id"] == ALICE, (
        "the audit log must say whose sign-in was removed"
    )
