"""An invitation must be accepted before an org admin has any power over
the invited person, and before the person has any access to the org.

Inviting puts an address in the org's users; only the invited person's
own Join (``POST /api/invitations/{slug}/accept``) turns it into a
membership. Signing in never does. Before this rule, the first sign-in
(anywhere) accepted every invitation to the address, so any org admin
could invite ``victim@x`` and then revoke or remove them: that signed
the victim out of every org they really belong to, and the answer told
the admin whether the victim used MCP Hero at all.

Full-app tests (``make_test_client``: standalone, dev-stub sign-in). Its
config users ``admin@example.com`` and ``dev@example.com`` are members.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.domain.model.settings import RoleDefinition, SettingsConfig, UserDefinition
from mcpolis.domain.services.policy_engine import PolicyEngine
from tests.unit._dev_stub_login import accept_invitation, login_as
from tests.unit.test_dashboard_api import (
    add_open_gateway_session,
    make_test_client,
    seed_gateway_tokens,
)

ADMIN = "admin@example.com"
STRANGER = "stranger@elsewhere.example"
INVITEE = "invitee@example.com"


def invite(client: TestClient, email: str, role: str = "developer") -> None:
    resp = client.post("/api/admin/users", json={"email": email, "role": role})
    assert resp.status_code == 201, resp.text


def gateway_tokens_of(client: TestClient, email: str) -> int:
    provider = client.app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined]
    access = sum(
        1 for t in provider._access_tokens.values() if t.user_email == email  # pyright: ignore[reportPrivateUsage]
    )
    refresh = sum(
        1 for t in provider._refresh_tokens.values() if t.user_email == email  # pyright: ignore[reportPrivateUsage]
    )
    return access + refresh


def team_status(client: TestClient, email: str) -> str | None:
    users = client.get("/api/admin/users").json()
    return next((u["status"] for u in users if u["email"] == email), None)


def make_upstream_sign_in(access_token: str) -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=[],
    )


def dev_stub_sign_in(client: TestClient, email: str, join: str | None = None) -> str:
    """Walk the dev-stub sign-in (optionally from a join link) and return
    where the callback sends the browser."""
    login_resp = client.get(
        "/api/auth/login",
        params={"join": join} if join else None,
        follow_redirects=False,
    )
    state = parse_qs(urlparse(login_resp.headers["location"]).query)["state"][0]
    submit = client.get(
        "/api/auth/dev-stub/submit",
        params={
            "email": email,
            "state": state,
            "redirect_uri": "http://testserver/api/auth/callback",
        },
        follow_redirects=False,
    )
    callback = client.get(submit.headers["location"], follow_redirects=False)
    assert callback.status_code == 302, callback.text
    return callback.headers["location"]


# --- The admin has no power over a pending invitation ---


def test_inviting_then_revoking_leaves_an_outsiders_gateway_sign_in(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    seed_gateway_tokens(client, STRANGER)  # signed in through ANOTHER org
    terminate = add_open_gateway_session(client, STRANGER, "s-stranger")
    invite(client, STRANGER)

    resp = client.delete("/api/admin/gateway/users/stranger%40elsewhere.example")

    assert resp.status_code == 404, resp.text
    assert gateway_tokens_of(client, STRANGER) == 3
    terminate.assert_not_awaited()


def test_removing_a_pending_invitation_only_deletes_the_invitation(
    tmp_path: Path,
) -> None:
    """No gateway revoke, no session close, no sign-in deletion: the
    invited person is not a member, so nothing of theirs is touched."""
    client = make_test_client(tmp_path)
    seed_gateway_tokens(client, STRANGER)
    terminate = add_open_gateway_session(client, STRANGER, "s-stranger")
    invite(client, STRANGER)
    store = FileConnectionStore(tmp_path / "data")
    asyncio.run(store.put_user_token(
        "default", STRANGER, "mixpanel", make_upstream_sign_in("theirs"),
    ))

    resp = client.delete("/api/admin/users/stranger%40elsewhere.example")

    assert resp.status_code == 200, resp.text
    assert team_status(client, STRANGER) is None
    assert gateway_tokens_of(client, STRANGER) == 3
    terminate.assert_not_awaited()
    assert asyncio.run(
        store.get_user_token("default", STRANGER, "mixpanel"),
    ) is not None


def test_revoking_a_pending_invitation_tells_nothing_about_the_person(
    tmp_path: Path,
) -> None:
    """An invited address with gateway tokens and one without get the
    same answer: no "is this a MCP Hero user" oracle."""
    client = make_test_client(tmp_path)
    seed_gateway_tokens(client, STRANGER)
    invite(client, STRANGER)
    invite(client, "nobody@elsewhere.example")

    with_tokens = client.delete(
        "/api/admin/gateway/users/stranger%40elsewhere.example",
    )
    without_tokens = client.delete(
        "/api/admin/gateway/users/nobody%40elsewhere.example",
    )

    assert (with_tokens.status_code, without_tokens.status_code) == (404, 404)
    assert with_tokens.json()["detail"].replace(STRANGER, "X") == (
        without_tokens.json()["detail"].replace("nobody@elsewhere.example", "X")
    )


def test_inviting_an_outsider_does_not_show_their_gateway_sign_in(
    tmp_path: Path,
) -> None:
    """The org's "connected users" lists members only: inviting an
    address signed in through another org must not reveal it."""
    client = make_test_client(tmp_path)
    seed_gateway_tokens(client, STRANGER)
    seed_gateway_tokens(client, "dev@example.com")
    invite(client, STRANGER)

    gateway = client.get("/api/config/gateway").json()

    assert gateway["connected_users"] == ["dev@example.com"]
    assert STRANGER not in gateway["all_users"]


# --- The invited person has no access until they accept ---


def test_signing_in_does_not_accept_an_invitation(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    invite(client, INVITEE)

    landing = dev_stub_sign_in(client, INVITEE)

    # Standalone has one org: the invited person lands on its Join page.
    assert landing == "/orgs/default/join"
    me = client.get("/api/auth/me").json()
    assert me["orgs"] == []
    assert me["invitations"] == [
        {"slug": "default", "display_name": "Default", "role": "developer"},
    ]
    # Signed in, but the org's own pages refuse them: not a member yet.
    assert client.get("/api/config/gateway").status_code == 403
    assert client.get("/api/user/mcps").status_code == 403
    assert client.get("/api/admin/users").status_code == 403
    login_as(client, ADMIN)
    assert team_status(client, INVITEE) == "pending"


def test_an_invitation_typed_with_capitals_can_be_accepted(tmp_path: Path) -> None:
    """The admin types ``Invitee@Example.com``; Google reports
    ``invitee@example.com``. Letter case carries no meaning, so the
    invited person finds their invitation, joins, and is a member."""
    client = make_test_client(tmp_path)
    invite(client, "Invitee@Example.com")

    landing = dev_stub_sign_in(client, INVITEE)

    assert landing == "/orgs/default/join"
    me = client.get("/api/auth/me").json()
    assert [i["slug"] for i in me["invitations"]] == ["default"]
    assert client.post("/api/invitations/default/accept").status_code == 200
    assert client.get("/api/user/mcps").status_code == 200
    assert client.get("/api/auth/me").json()["invitations"] == []
    login_as(client, ADMIN)
    assert team_status(client, "Invitee@Example.com") == "active"


def test_a_member_invited_with_capitals_shows_as_connected(tmp_path: Path) -> None:
    """Their gateway sign-in is kept under the address they signed in
    with: the org's "connected users" still finds them."""
    client = make_test_client(tmp_path)
    invite(client, "Invitee@Example.com")
    accept_invitation(client, INVITEE)
    seed_gateway_tokens(client, INVITEE)

    gateway = client.get("/api/config/gateway").json()

    assert "Invitee@Example.com" in gateway["connected_users"]


def test_an_invitation_typed_with_capitals_can_be_declined(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    invite(client, "Invitee@Example.com")
    login_as(client, INVITEE)

    resp = client.post("/api/invitations/default/decline")

    assert resp.status_code == 200, resp.text
    login_as(client, ADMIN)
    assert team_status(client, "Invitee@Example.com") is None


def test_a_join_link_sign_in_lands_an_invited_person_on_the_join_page(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    invite(client, INVITEE)

    assert dev_stub_sign_in(client, INVITEE, join="default") == (
        "/orgs/default/join"
    )
    assert dev_stub_sign_in(client, "dev@example.com", join="default") == "/"


def test_a_pending_admin_invitation_grants_no_admin_rights(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    invite(client, INVITEE, role="admin")

    login_as(client, INVITEE)

    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/orgs/default/switch").status_code == 401


def test_accepting_the_invitation_makes_the_person_a_member(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    invite(client, INVITEE, role="admin")

    accept_invitation(client, INVITEE)

    me = client.get("/api/auth/me").json()
    assert [o["slug"] for o in me["orgs"]] == ["default"]
    assert me["invitations"] == []
    assert client.post("/api/orgs/default/switch").status_code == 204
    assert client.get("/api/admin/users").status_code == 200
    assert team_status(client, INVITEE) == "active"


def test_accepting_twice_is_harmless(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    invite(client, INVITEE)
    accept_invitation(client, INVITEE)

    again = client.post("/api/invitations/default/accept")

    assert again.status_code == 200, again.text
    login_as(client, ADMIN)
    assert team_status(client, INVITEE) == "active"


def test_an_invitation_removed_meanwhile_cannot_be_accepted(
    tmp_path: Path,
) -> None:
    """The admin removes the invitation while the invited person is on
    the Join page: their Join finds nothing to accept."""
    invitee = make_test_client(tmp_path)
    invite(invitee, INVITEE)
    admin = TestClient(invitee.app)
    login_as(admin, ADMIN)
    login_as(invitee, INVITEE)

    removed = admin.delete("/api/admin/users/invitee%40example.com")
    assert removed.status_code == 200, removed.text

    assert invitee.post("/api/invitations/default/accept").status_code == 404
    assert invitee.post("/api/invitations/no-such-org/accept").status_code == 404
    assert team_status(admin, INVITEE) is None


def test_declining_deletes_the_invitation(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    invite(client, INVITEE)
    login_as(client, INVITEE)

    resp = client.post("/api/invitations/default/decline")

    assert resp.status_code == 200, resp.text
    assert client.get("/api/auth/me").json()["invitations"] == []
    login_as(client, ADMIN)
    assert team_status(client, INVITEE) is None


def test_a_member_cannot_decline_their_membership(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    login_as(client, "dev@example.com")

    resp = client.post("/api/invitations/default/decline")

    assert resp.status_code == 409, resp.text
    login_as(client, ADMIN)
    assert team_status(client, "dev@example.com") == "active"


def make_policy(members: list[str]) -> PolicyEngine:
    config = SettingsConfig(
        roles={
            "admin": RoleDefinition(is_admin=True),
            "user": RoleDefinition(is_default=True),
        },
        users={
            "alice@co.com": UserDefinition(role="admin"),
            "bob@co.com": UserDefinition(role="admin"),
        },
    )
    return PolicyEngine(config, members)


def test_a_pending_invitation_has_no_role_until_accepted() -> None:
    """The running policy behind the gateway, the Admin MCP and the
    dashboard: an invited address resolves to no role (no tools, no
    admin rights) until it accepts. Letter case doesn't matter; the
    admins are listed as each accepted (their row's spelling)."""
    policy = make_policy(["Alice@Co.com"])

    assert policy.is_member("alice@co.com")
    assert policy.get_admin_emails() == ["Alice@Co.com"]
    assert not policy.is_member("bob@co.com")
    assert policy.get_user_roles("bob@co.com") == []
    assert not policy.is_admin("bob@co.com")

    policy.add_member("bob@co.com")
    assert policy.get_user_roles("bob@co.com") == ["admin"]
    policy.discard_member("bob@co.com")
    assert not policy.is_admin("bob@co.com")
