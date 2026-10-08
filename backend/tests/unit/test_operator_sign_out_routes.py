"""Operator actions that sign a user out (``superadmin_routes.py``).

Two operator-only buttons of the cross-org dashboard, gated by
``MCPOLIS_SUPERADMIN_EMAILS``:

- ``POST /api/superadmin/users/{email}/sessions/revoke`` signs the user out
  of the gateway: every gateway access and refresh token of that email is
  deleted, so each of their MCP clients has to sign in again.
- ``POST /api/superadmin/users/{email}/connections/{org}/{upstream}/reauth``
  deletes the user's saved sign-in to one MCP in one org, so their next use
  of that MCP asks them to sign in to it again.

A signed-in caller who is not on the operator list gets 403, a signed-out
caller 401, and in both cases nothing is deleted.

Uses the standalone ``create_app`` + dev-stub login harness of
``test_superadmin_upstream_liveness.py``. The saved sign-ins are written and
read back through the app's own gateway OAuth provider and connection store,
the same objects the routes act on.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
    OAuthToken,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as
from tests.unit.factories import make_config_users_accepted

# The operator is a plain member of the org: the power comes from the
# operator list, not from the org role.
OPERATOR_EMAIL = "ops@example.com"
TEAM_ADMIN_EMAIL = "teamadmin@example.com"
TARGET_EMAIL = "target@example.com"
BYSTANDER_EMAIL = "bystander@example.com"
NOTION = "notion"
LINEAR = "linear"


def make_settings(
    tmp_path: Path, *, superadmin_emails: str = OPERATOR_EMAIL,
) -> Settings:
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {
        NOTION: {"url": "https://notion.example.invalid/mcp"},
        LINEAR: {"url": "https://linear.example.invalid/mcp"},
    }}))
    config_path = tmp_path / "config.json"
    config_text = json.dumps({
        "upstreams": {
            NOTION: {"display_name": "Notion", "auth_mode": "per_user_oauth"},
            LINEAR: {"display_name": "Linear", "auth_mode": "per_user_oauth"},
        },
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {
            OPERATOR_EMAIL: {"role": "user"},
            TEAM_ADMIN_EMAIL: {"role": "admin"},
            TARGET_EMAIL: {"role": "user"},
            BYSTANDER_EMAIL: {"role": "user"},
        },
    })
    config_path.write_text(config_text)
    # Everyone accepted their invitation: a pending one gives no access.
    make_config_users_accepted(tmp_path / "data", config_text)
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_path,
        config_path=config_path,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
        superadmin_emails=superadmin_emails,
    )


def make_app(tmp_path: Path, *, superadmin_emails: str = OPERATOR_EMAIL) -> FastAPI:
    return create_app(make_settings(tmp_path, superadmin_emails=superadmin_emails))


def make_client(app: FastAPI, *, login: str | None) -> TestClient:
    """A browser on the dashboard; ``login=None`` stays signed out."""
    client = TestClient(app, raise_server_exceptions=True)
    if login is not None:
        login_as(client, login)
    return client


def gateway_sign_ins(app: FastAPI) -> McpGatewayOAuthProvider:
    """Where the app keeps gateway sign-ins (the MCP clients' tokens)."""
    return cast(McpGatewayOAuthProvider, app.state.mcp_gateway_oauth_provider)


def saved_mcp_sign_ins(app: FastAPI) -> ConnectionStore:
    """Where the app keeps each user's saved sign-in to each MCP."""
    return cast(ConnectionStore, app.state.connection_store)


def make_saved_mcp_sign_in(label: str) -> OAuthToken:
    return OAuthToken(
        access_token=f"{label}-access",
        refresh_token=f"{label}-refresh",
        expires_at=None,
        scopes=[],
    )


def sign_out_everywhere_url(email: str) -> str:
    return f"/api/superadmin/users/{email}/sessions/revoke"


def clear_mcp_sign_in_url(email: str, org_id: str, upstream_id: str) -> str:
    return (
        f"/api/superadmin/users/{email}/connections/{org_id}/{upstream_id}"
        "/reauth"
    )


async def seed_target_sign_ins(app: FastAPI) -> str:
    """Sign the target in to the gateway and to Notion; returns the
    gateway token their MCP client holds."""
    await saved_mcp_sign_ins(app).put_user_token(
        DEFAULT_ORG_ID, TARGET_EMAIL, NOTION, make_saved_mcp_sign_in("target"),
    )
    return await gateway_sign_ins(app).mint_test_token(TARGET_EMAIL)


async def assert_target_still_signed_in(app: FastAPI, gateway_token: str) -> None:
    assert await gateway_sign_ins(app).load_access_token(gateway_token) is not None
    assert TARGET_EMAIL in gateway_sign_ins(app).get_connected_users()
    assert await saved_mcp_sign_ins(app).get_user_token(
        DEFAULT_ORG_ID, TARGET_EMAIL, NOTION,
    ) is not None


async def test_operator_signs_a_user_out_of_every_mcp_client(
    tmp_path: Path,
) -> None:
    """Sign out everywhere deletes the user's gateway access and refresh
    tokens, so none of their MCP clients stays signed in; other users keep
    their sign-ins."""
    app = make_app(tmp_path)
    gateway = gateway_sign_ins(app)
    target_token = await gateway.mint_test_token(TARGET_EMAIL)
    bystander_token = await gateway.mint_test_token(BYSTANDER_EMAIL)
    operator = make_client(app, login=OPERATOR_EMAIL)

    resp = operator.post(sign_out_everywhere_url(TARGET_EMAIL))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == TARGET_EMAIL
    assert body["gateway_tokens_revoked"] == 2  # one access + one refresh token
    assert await gateway.load_access_token(target_token) is None
    # The connected-users list counts refresh tokens too: none is left.
    assert TARGET_EMAIL not in gateway.get_connected_users()
    assert await gateway.load_access_token(bystander_token) is not None


async def test_operator_clears_one_saved_mcp_sign_in(tmp_path: Path) -> None:
    """Clearing a sign-in deletes the user's saved sign-in to that one MCP
    in that org; their other MCPs and other users' sign-ins stay."""
    app = make_app(tmp_path)
    store = saved_mcp_sign_ins(app)
    await store.put_user_token(
        DEFAULT_ORG_ID, TARGET_EMAIL, NOTION, make_saved_mcp_sign_in("t-notion"),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, TARGET_EMAIL, LINEAR, make_saved_mcp_sign_in("t-linear"),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, BYSTANDER_EMAIL, NOTION, make_saved_mcp_sign_in("b-notion"),
    )
    operator = make_client(app, login=OPERATOR_EMAIL)

    resp = operator.post(
        clear_mcp_sign_in_url(TARGET_EMAIL, DEFAULT_ORG_ID, NOTION),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] is True
    assert await store.get_user_token(DEFAULT_ORG_ID, TARGET_EMAIL, NOTION) is None
    assert await store.get_user_token(
        DEFAULT_ORG_ID, TARGET_EMAIL, LINEAR,
    ) is not None
    assert await store.get_user_token(
        DEFAULT_ORG_ID, BYSTANDER_EMAIL, NOTION,
    ) is not None


async def test_operator_listed_with_capitals_keeps_operator_powers(
    tmp_path: Path,
) -> None:
    """The operator list ignores letter case: listed as ``Ops@Example.com``,
    the operator who signs in as ``ops@example.com`` still sees the
    operator pages and can still sign a user out."""
    app = make_app(tmp_path, superadmin_emails="Ops@Example.com")
    target_token = await gateway_sign_ins(app).mint_test_token(TARGET_EMAIL)
    operator = make_client(app, login=OPERATOR_EMAIL)

    me = operator.get("/api/auth/me")
    resp = operator.post(sign_out_everywhere_url(TARGET_EMAIL))

    assert me.status_code == 200, me.text
    assert me.json()["is_superadmin"] is True
    assert resp.status_code == 200, resp.text
    assert await gateway_sign_ins(app).load_access_token(target_token) is None


async def test_team_admin_not_on_the_operator_list_cannot_sign_anyone_out(
    tmp_path: Path,
) -> None:
    """Being an admin of the org is not enough: both actions answer 403 to
    a signed-in caller missing from the operator list, and the target keeps
    every sign-in."""
    app = make_app(tmp_path)
    gateway_token = await seed_target_sign_ins(app)
    team_admin = make_client(app, login=TEAM_ADMIN_EMAIL)

    responses = [
        team_admin.post(sign_out_everywhere_url(TARGET_EMAIL)),
        team_admin.post(
            clear_mcp_sign_in_url(TARGET_EMAIL, DEFAULT_ORG_ID, NOTION),
        ),
    ]

    for resp in responses:
        assert resp.status_code == 403, resp.text
        assert resp.json() == {"detail": "Superadmin role required"}
    await assert_target_still_signed_in(app, gateway_token)


async def test_signed_out_caller_cannot_sign_anyone_out(tmp_path: Path) -> None:
    """Without a dashboard sign-in both actions answer 401, and the target
    keeps every sign-in."""
    app = make_app(tmp_path)
    gateway_token = await seed_target_sign_ins(app)
    anonymous = make_client(app, login=None)

    responses = [
        anonymous.post(sign_out_everywhere_url(TARGET_EMAIL)),
        anonymous.post(
            clear_mcp_sign_in_url(TARGET_EMAIL, DEFAULT_ORG_ID, NOTION),
        ),
    ]

    for resp in responses:
        assert resp.status_code == 401, resp.text
        assert resp.json() == {"detail": "Not authenticated"}
    await assert_target_still_signed_in(app, gateway_token)


async def test_clearing_a_sign_in_in_an_unknown_org_answers_not_found(
    tmp_path: Path,
) -> None:
    """An operator who names an org that does not exist gets 404, not a
    success message for a sign-in that was never there."""
    app = make_app(tmp_path)
    operator = make_client(app, login=OPERATOR_EMAIL)

    resp = operator.post(clear_mcp_sign_in_url(TARGET_EMAIL, "no-such-org", NOTION))

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Organization not found"}
