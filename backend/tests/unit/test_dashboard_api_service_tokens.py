"""REST API tests for the service-token admin endpoints.

Pins the wire shape of:

- ``GET    /api/admin/service-tokens`` — ``[ServiceTokenInfo]``,
  never the hash, never the raw value.
- ``POST   /api/admin/service-tokens`` — 201 with the raw token
  exactly once; 400 invalid label / unknown role; 409 duplicate.
- ``DELETE /api/admin/service-tokens/{label}`` — revoke or 404.

Plus the boundary invariants: non-admins 403, anonymous 401, a
``svct_`` bearer means nothing on the dashboard API (cookie-only),
the admin MCP rejects service tokens, and svc identities never
appear in the Team user list.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as

MCP_JSON = json.dumps({
    "mcpServers": {
        "github": {"url": "http://localhost:9000/mcp"},
    }
})

CONFIG_JSON = json.dumps({
    "upstreams": {
        "github": {"display_name": "GitHub", "auth_mode": "service_account"},
    },
    "roles": {
        "admin": {
            "is_admin": True,
            "settings": {"mcp_access": {"mcps": {"github": True}}},
        },
        "user": {
            "is_default": True,
            "settings": {"mcp_access": {"mcps": {"github": True}}},
        },
        # No users assigned — exists for the role-deletion-guard test
        # (custom role creation is plan-gated on the free tier).
        "spare": {
            "settings": {"mcp_access": {"mcps": {}}},
        },
    },
    "users": {
        "admin@example.com": {"role": "admin"},
        "member@example.com": {"role": "user"},
    },
})


def make_test_client(
    tmp_path: Path, *, login: str | None = "admin@example.com",
) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(MCP_JSON)
    config = tmp_path / "config.json"
    config.write_text(CONFIG_JSON)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=True)
    if login is not None:
        login_as(client, login)
    return client


def mint(
    client: TestClient, label: str = "ci-bot", role: str = "user",
) -> dict[str, object]:
    resp = client.post(
        "/api/admin/service-tokens",
        json={"label": label, "role": role},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- mint + list ---


def test_mint_returns_raw_token_once_and_list_omits_it(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    body = mint(client)
    token = body["token"]
    assert isinstance(token, str)
    assert token.startswith("svct_")
    info = body["info"]
    assert info["label"] == "ci-bot"  # type: ignore[index]
    assert info["role"] == "user"  # type: ignore[index]
    assert info["created_by"] == "admin@example.com"  # type: ignore[index]

    listed = client.get("/api/admin/service-tokens")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["label"] == "ci-bot"
    assert rows[0]["last_used_at"] is None
    # Neither the raw token nor any hash-like field leaks.
    assert token not in listed.text
    assert "token_hash" not in rows[0]
    assert "token" not in rows[0]


def test_mint_duplicate_label_409(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    mint(client)
    resp = client.post(
        "/api/admin/service-tokens",
        json={"label": "ci-bot", "role": "user"},
    )
    assert resp.status_code == 409


def test_mint_unknown_role_400(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/service-tokens",
        json={"label": "ci-bot", "role": "no-such-role"},
    )
    assert resp.status_code == 400
    assert "Role" in resp.json()["detail"]


def test_mint_invalid_label_400(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    for bad in ("Has Spaces", "UPPER", "-leading-dash", "x" * 65, ""):
        resp = client.post(
            "/api/admin/service-tokens",
            json={"label": bad, "role": "user"},
        )
        assert resp.status_code == 400, bad


# --- authz ---


def test_non_admin_user_403(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, login="member@example.com")
    assert client.get("/api/admin/service-tokens").status_code == 403
    resp = client.post(
        "/api/admin/service-tokens",
        json={"label": "ci-bot", "role": "user"},
    )
    assert resp.status_code == 403


def test_unauthenticated_401(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, login=None)
    assert client.get("/api/admin/service-tokens").status_code == 401


def test_dashboard_api_ignores_service_token_bearer(tmp_path: Path) -> None:
    """Dashboard auth is cookie-only; a gateway bearer means nothing."""
    client = make_test_client(tmp_path)
    token = mint(client)["token"]
    fresh = make_test_client(tmp_path, login=None)
    resp = fresh.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


def test_admin_mcp_rejects_service_token(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    token = mint(client)["token"]
    resp = client.post(
        "/admin-mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
    )
    # The admin app wraps the raw OAuth provider — a svct_ bearer
    # never authenticates there.
    assert resp.status_code == 401


# --- revoke ---


def test_revoke_then_list_empty(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    mint(client)
    resp = client.delete("/api/admin/service-tokens/ci-bot")
    assert resp.status_code == 200
    assert resp.json() == {"status": "revoked"}
    assert client.get("/api/admin/service-tokens").json() == []


def test_revoke_unknown_label_404(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    assert (
        client.delete("/api/admin/service-tokens/no-such").status_code == 404
    )


# --- isolation from the human user surfaces ---


def test_service_identity_never_in_team_user_list(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    mint(client)
    users = client.get("/api/admin/users").json()
    emails = [u["email"] for u in users]
    assert emails == ["admin@example.com", "member@example.com"]
    assert not any(e.startswith("svc:") for e in emails)


# --- roles surface: counts + deletion guard ---


def test_roles_list_includes_service_token_count(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    mint(client, label="bot-a", role="user")
    mint(client, label="bot-b", role="user")
    roles = {r["name"]: r for r in client.get("/api/admin/roles").json()}
    assert roles["user"]["service_token_count"] == 2
    assert roles["admin"]["service_token_count"] == 0
    # user_count is unaffected by tokens (they are not seats).
    assert roles["user"]["user_count"] == 1


def test_role_referenced_by_token_is_not_deletable(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    # "spare" has no users — deletable in principle...
    mint(client, label="bot", role="spare")
    # ...but not while a token references it.
    resp = client.delete("/api/admin/roles/spare")
    assert resp.status_code == 400
    assert "service token" in resp.json()["detail"]
    # Revoking the token unblocks deletion.
    assert client.delete("/api/admin/service-tokens/bot").status_code == 200
    assert client.delete("/api/admin/roles/spare").status_code == 200
