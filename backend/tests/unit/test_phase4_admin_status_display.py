"""Phase A — admin tab + /my-tools surfaces report ``ready`` and
``slot_owner`` consistently.

The dashboard's admin tab list/detail and the /my-tools endpoint now
share a single readiness model:

- ``ready`` (org-level): Ready ⇔ at least one admin authenticated for
  OAuth modes; shared session live for ``service_account``.
- ``slot_owner``: email of the admin currently providing readiness
  for either OAuth mode (most-recent-updated row wins for
  ``per_user_oauth`` multi-admin); ``None`` for ``service_account``
  or when no admin is signed in.

These tests build a real FastAPI app with file-backed storage, seed
tokens directly into the connection store, and assert the HTTP
responses. Following CLAUDE.md: top-level functions, no fixtures,
common setup factored into ``make_*`` helpers.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as InternalOAuthToken,
)
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as


MCP_JSON = json.dumps({
    "mcpServers": {
        "slack": {"url": "http://localhost:9000/mcp"},
        "notion": {"url": "http://localhost:9001/mcp"},
    },
})

CONFIG_JSON = json.dumps({
    "upstreams": {
        "slack": {"display_name": "Slack", "auth_mode": "admin_oauth"},
        "notion": {"display_name": "Notion", "auth_mode": "per_user_oauth"},
    },
    "roles": {
        "admin": {
            "is_admin": True,
            "settings": {
                "mcp_access": {"mcps": {"slack": True, "notion": True}},
            },
        },
        "user": {
            "is_default": True,
            "settings": {
                "mcp_access": {"mcps": {"slack": True, "notion": True}},
            },
        },
    },
    "users": {
        "alice@co.com": {"role": "admin"},
        "bob@co.com": {"role": "admin"},
        "carol@co.com": {"role": "user"},
    },
})


def make_token(
    access_token: str = "x", updated_at: datetime | None = None,
) -> InternalOAuthToken:
    if updated_at is None:
        updated_at = datetime.now(UTC)
    return InternalOAuthToken(
        access_token=access_token,
        refresh_token=None,
        expires_at=updated_at + timedelta(hours=1),
        scopes=[],
        updated_at=updated_at,
    )


def make_app_and_store(tmp_path: Path) -> tuple[TestClient, FileConnectionStore]:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(MCP_JSON)
    config = tmp_path / "config.json"
    config.write_text(CONFIG_JSON)
    data_dir = tmp_path / "data"
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager."
        "UpstreamClientManager.start_all",
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all",
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=True)
    store = FileConnectionStore(data_dir)
    return client, store


# ---------------------------------------------------------------------
# Admin tab list — slot_owner per mode
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_slot_owner_set_when_admin_oauth_admin_connected(
    tmp_path: Path,
) -> None:
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token(),
    )

    login_as(client, "bob@co.com")
    resp = client.get("/api/admin/upstreams")
    assert resp.status_code == 200
    rows = resp.json()
    slack = next(r for r in rows if r["id"] == "slack")
    assert slack["ready"] is True
    assert slack["slot_owner"] == "alice@co.com"


@pytest.mark.asyncio
async def test_summary_admin_oauth_not_ready_when_no_admin_connected(
    tmp_path: Path,
) -> None:
    client, _ = make_app_and_store(tmp_path)
    login_as(client, "alice@co.com")
    resp = client.get("/api/admin/upstreams")
    assert resp.status_code == 200
    rows = resp.json()
    slack = next(r for r in rows if r["id"] == "slack")
    assert slack["ready"] is False
    assert slack["slot_owner"] is None


@pytest.mark.asyncio
async def test_summary_per_user_oauth_uniform_with_admin_oauth(
    tmp_path: Path,
) -> None:
    """Under the new design, ``per_user_oauth`` reports ``ready`` +
    ``slot_owner`` the same way ``admin_oauth`` does — admin's signed
    in row makes the upstream Ready and surfaces them as ``slot_owner``.
    """
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )

    login_as(client, "alice@co.com")
    resp = client.get("/api/admin/upstreams")
    assert resp.status_code == 200
    rows = resp.json()
    notion = next(r for r in rows if r["id"] == "notion")
    assert notion["ready"] is True
    assert notion["slot_owner"] == "alice@co.com"


@pytest.mark.asyncio
async def test_summary_per_user_oauth_only_non_admin_signed_in(
    tmp_path: Path,
) -> None:
    """Load-bearing rule: a non-admin's row does NOT make the upstream
    Ready. Admin presence is the anchor; without an admin signed in,
    ``ready=False`` and ``slot_owner=None`` even if non-admins have
    personal tokens."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion", make_token(),
    )

    login_as(client, "alice@co.com")
    resp = client.get("/api/admin/upstreams")
    assert resp.status_code == 200
    rows = resp.json()
    notion = next(r for r in rows if r["id"] == "notion")
    assert notion["ready"] is False
    assert notion["slot_owner"] is None


# ---------------------------------------------------------------------
# Admin tab detail — slot_owner + connected_users
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_returns_connected_users_with_metadata(
    tmp_path: Path,
) -> None:
    client, store = make_app_and_store(tmp_path)
    expiry = datetime.now(UTC) + timedelta(hours=2)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
        InternalOAuthToken(
            access_token="alice", refresh_token=None,
            expires_at=expiry, scopes=[],
        ),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion", make_token(),
    )

    login_as(client, "alice@co.com")
    resp = client.get("/api/admin/upstreams/notion")
    assert resp.status_code == 200
    data = resp.json()
    by_email = {u["email"]: u for u in data["connected_users"]}
    assert by_email["alice@co.com"]["is_admin"] is True
    assert by_email["carol@co.com"]["is_admin"] is False
    assert by_email["alice@co.com"]["expires_at"] is not None


@pytest.mark.asyncio
async def test_detail_slot_owner_set_when_admin_oauth_admin_connected(
    tmp_path: Path,
) -> None:
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token(),
    )

    login_as(client, "bob@co.com")
    resp = client.get("/api/admin/upstreams/slack")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert data["slot_owner"] == "alice@co.com"
    assert {u["email"] for u in data["connected_users"]} == {"alice@co.com"}


@pytest.mark.asyncio
async def test_detail_per_user_oauth_uniform_slot_owner(
    tmp_path: Path,
) -> None:
    """Detail endpoint: per_user_oauth reports a slot_owner the same
    way admin_oauth does — admin's row sets ready=True and
    slot_owner=admin email."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )
    # Non-admin's row coexists; the admin's row provides the slot.
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion", make_token(),
    )

    login_as(client, "alice@co.com")
    resp = client.get("/api/admin/upstreams/notion")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert data["slot_owner"] == "alice@co.com"


# ---------------------------------------------------------------------
# Take-over conflict (existing 409 behaviour preserved across schema)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_admin_oauth_returns_409_when_other_admin_owns_slot(
    tmp_path: Path,
) -> None:
    """End-to-end: admin connect on admin_oauth fails with 409 when
    another admin already owns the slot. Asserting against the new
    schema's slot_owner field shape is covered by the summary tests
    above; this test pins the behaviour itself."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token(),
    )

    login_as(client, "bob@co.com")
    resp = client.post("/api/admin/upstreams/slack/connect")
    assert resp.status_code == 409
    assert "alice@co.com" in resp.json()["detail"].lower()
    assert "disconnect" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_connect_per_user_oauth_returns_409_when_other_admin_owns_slot(
    tmp_path: Path,
) -> None:
    """Phase B symmetry: the admin-tab take-over conflict applies to
    per_user_oauth too. When admin alice has a row for ``notion``,
    admin bob's connect from the admin tab returns 409 instead of
    silently writing a second admin row alongside hers. Same shape
    as the admin_oauth conflict test above."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )

    login_as(client, "bob@co.com")
    resp = client.post("/api/admin/upstreams/notion/connect")
    assert resp.status_code == 409
    assert "alice@co.com" in resp.json()["detail"].lower()
    assert "disconnect" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_disconnect_per_user_oauth_admin_clears_slot_owner_keeps_users(
    tmp_path: Path,
) -> None:
    """Phase B: the admin-tab disconnect on a per_user_oauth upstream
    clears the slot-owning admin's row (so a subsequent take-over by
    another admin can succeed) without touching non-admin users'
    independent rows.
    """
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion", make_token(),
    )

    login_as(client, "bob@co.com")
    resp = client.post("/api/admin/upstreams/notion/disconnect")
    assert resp.status_code == 200

    alice_token = await store.get_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
    )
    carol_token = await store.get_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion",
    )
    assert alice_token is None
    assert carol_token is not None  # non-admin row preserved


@pytest.mark.asyncio
async def test_disconnect_admin_oauth_clears_active_admin_token(
    tmp_path: Path,
) -> None:
    """Take-over: bob calls disconnect on slack while alice owns the
    slot. Alice's token must be cleared so the subsequent connect from
    bob succeeds."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token(),
    )

    login_as(client, "bob@co.com")
    resp = client.post("/api/admin/upstreams/slack/disconnect")
    assert resp.status_code == 200

    cleared = await store.get_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack",
    )
    assert cleared is None


@pytest.mark.asyncio
async def test_disconnect_per_user_oauth_only_clears_callers_own_token(
    tmp_path: Path,
) -> None:
    """The /api/auth/disconnect endpoint clears only the calling
    user's token. A user disconnecting from notion must not affect
    another user's stored row."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion", make_token(),
    )

    login_as(client, "carol@co.com")
    resp = client.post("/api/auth/disconnect/notion")
    assert resp.status_code == 200

    alice_token = await store.get_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
    )
    carol_token = await store.get_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion",
    )
    assert alice_token is not None
    assert carol_token is None


# ---------------------------------------------------------------------
# /my-tools — ready vs user_connection_status separation
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_mcps_admin_oauth_ready_when_admin_pool_populated(
    tmp_path: Path,
) -> None:
    """Regression for the post-Phase-6 ``/my-tools`` Unavailable bug
    rewritten under the new schema. An admin with stored tokens makes
    the row Ready for *every* logged-in user; the user_connection
    status mirrors that (admin_oauth doesn't have a per-viewer
    sign-in concept)."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token(),
    )

    login_as(client, "bob@co.com")
    resp = client.get("/api/user/mcps")
    assert resp.status_code == 200
    rows = resp.json()
    slack = next(r for r in rows if r["id"] == "slack")
    assert slack["auth_mode"] == "admin_oauth"
    assert slack["ready"] is True
    assert slack["user_connection_status"] == "connected"


@pytest.mark.asyncio
async def test_user_mcps_per_user_oauth_signed_in_independent_of_ready(
    tmp_path: Path,
) -> None:
    """Distinct concepts: org Ready (admin presence) vs viewer
    signed-in (caller's own row). Admin alice is signed in →
    ``ready=True``. Viewer is non-admin carol with no row →
    ``user_connection_status=not_connected``."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )

    login_as(client, "carol@co.com")
    resp = client.get("/api/user/mcps")
    assert resp.status_code == 200
    rows = resp.json()
    notion = next(r for r in rows if r["id"] == "notion")
    assert notion["auth_mode"] == "per_user_oauth"
    assert notion["ready"] is True
    assert notion["user_connection_status"] == "not_connected"


@pytest.mark.asyncio
async def test_user_mcps_per_user_oauth_signed_in_when_caller_has_row(
    tmp_path: Path,
) -> None:
    """When admin alice is signed in (Ready=True) and carol *also*
    signs in via /my-tools, carol's user_connection_status flips to
    'connected' — mirroring her own row, distinct from Ready."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion", make_token(),
    )

    login_as(client, "carol@co.com")
    resp = client.get("/api/user/mcps")
    rows = resp.json()
    notion = next(r for r in rows if r["id"] == "notion")
    assert notion["ready"] is True
    assert notion["user_connection_status"] == "connected"


# ---------------------------------------------------------------------
# Cross-surface invariant — ready value matches across endpoints
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_mcps_ready_matches_admin_view(
    tmp_path: Path,
) -> None:
    """``ready`` is an org-level concept — not viewer-scoped — so the
    same upstream produces the same ``ready`` value on
    ``/api/user/mcps`` and on ``/api/admin/upstreams``. This is the
    contract that lets the admin tab and /my-tools share one source
    of truth."""
    client, store = make_app_and_store(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )

    login_as(client, "alice@co.com")
    admin_resp = client.get("/api/admin/upstreams")
    user_resp = client.get("/api/user/mcps")
    admin_rows = admin_resp.json()
    user_rows = user_resp.json()

    admin_notion = next(r for r in admin_rows if r["id"] == "notion")
    user_notion = next(r for r in user_rows if r["id"] == "notion")
    assert admin_notion["ready"] == user_notion["ready"] is True


@pytest.mark.asyncio
async def test_admin_tab_door_and_my_tools_door_write_same_row(
    tmp_path: Path,
) -> None:
    """Admin tab and /my-tools are two doors to the same OAuth row.
    Admin signs in via admin tab → row exists. Same admin signs out
    via /my-tools (the user-scope disconnect endpoint) → same row
    deleted. This pins the "two doors, same closet" claim from the
    plan's Resolution section.
    """
    client, store = make_app_and_store(tmp_path)
    # Simulate the admin-tab connect having stored a row.
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token(),
    )
    pre = await store.get_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
    )
    assert pre is not None

    login_as(client, "alice@co.com")
    resp = client.post("/api/auth/disconnect/notion")
    assert resp.status_code == 200

    post = await store.get_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
    )
    assert post is None
