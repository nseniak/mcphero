"""REST API tests for the per-MCP Sandbox-files endpoints.

Pins the wire shape of:

- ``GET /api/admin/upstreams/{id}/sandbox-files`` — list summaries
  (no ``contents``).
- ``PUT /api/admin/upstreams/{id}/sandbox-files/{name}`` — accepts
  ``{contents, target_path}``, returns the post-write summary.
- ``DELETE /api/admin/upstreams/{id}/sandbox-files/{name}`` —
  idempotent.
- ``GET /api/admin/system-variables?transport=`` — read-only list
  of system-injected ``${...}`` Variables (v1: ``HOME`` for stdio,
  ``[]`` for HTTP — no sandbox filesystem).

Negative cases: invalid name → 400, name shadowing a system
Variable → 400, target_path with unknown ``${...}`` → 400,
collision with a Variable → 409.
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
        "gcp": {
            "command": "uvx",
            "args": ["mcp-server-gcp"],
        },
    },
})

CONFIG_JSON = json.dumps({
    "upstreams": {
        "gcp": {"display_name": "GCP", "auth_mode": "service_account"},
    },
    "roles": {
        "admin": {
            "is_admin": True,
            "settings": {"mcp_access": {"mcps": {"gcp": True}}},
        },
    },
    "users": {
        "admin@example.com": {"role": "admin"},
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


# --- list_sandbox_files ---


def test_list_sandbox_files_empty_for_new_upstream(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/gcp/sandbox-files")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sandbox_files_requires_admin(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, login=None)
    resp = client.get("/api/admin/upstreams/gcp/sandbox-files")
    assert resp.status_code in (401, 403)


# --- set_sandbox_file ---


def test_set_sandbox_file_round_trips_summary(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/gcp/sandbox-files/GCP_CRED",
        json={
            "contents": '{"type":"service_account"}',
            "target_path": "${HOME}/.config/gcloud/credentials.json",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "GCP_CRED"
    assert data["target_path"] == "${HOME}/.config/gcloud/credentials.json"
    # ``contents`` is never echoed in the summary.
    assert "contents" not in data
    assert data["size_bytes"] == len('{"type":"service_account"}')


def test_set_sandbox_file_rejects_invalid_name(tmp_path: Path) -> None:
    """Disallowed name characters (slashes, spaces, query chars) are
    rejected with a 400. The slug grammar is URL-safe by design so
    the storage key can sit inside the API route without escaping."""
    client = make_test_client(tmp_path)
    # Slash would split the URL path; rejected client-side or routed
    # to a different endpoint depending on FastAPI's matching, so
    # use a name that hits the validator.
    resp = client.put(
        "/api/admin/upstreams/gcp/sandbox-files/has%20space",
        json={"contents": "{}", "target_path": "${HOME}/x"},
    )
    assert resp.status_code == 400


def test_set_sandbox_file_accepts_lowercase_and_dashed_names(tmp_path: Path) -> None:
    """Relaxed slug grammar — lowercase + dashes + dots are now
    valid storage keys (file names don't substitute, so the historic
    UPPER_SNAKE constraint is gone)."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/gcp/sandbox-files/gcp-cred",
        json={
            "contents": "{}",
            "target_path": "${HOME}/x",
            "display_name": "GCP service account",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "gcp-cred"
    assert data["display_name"] == "GCP service account"


def test_set_sandbox_file_defaults_display_name_to_id_when_omitted(
    tmp_path: Path,
) -> None:
    """Legacy / scripted callers that don't carry a ``display_name``
    in the body still work — the server defaults display_name to
    the URL ``{name}`` so the listing has a label to render."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/gcp/sandbox-files/legacy",
        json={"contents": "{}", "target_path": "${HOME}/x"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "legacy"
    assert data["display_name"] == "legacy"


def test_set_sandbox_file_rejects_relative_path(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/gcp/sandbox-files/X",
        json={"contents": "y", "target_path": "relative/path"},
    )
    assert resp.status_code == 400


def test_set_sandbox_file_accepts_unknown_target_path_token(
    tmp_path: Path,
) -> None:
    """``target_path`` accepts any ``${...}`` reference at write time.

    Token resolution happens at launch; an unresolved reference
    surfaces as ``MissingTemplateVarError`` then, not as a 400 here.
    Allowing user-Variable references in ``target_path`` lets the
    operator parameterize per-tenant / per-profile paths.
    """
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/gcp/sandbox-files/X",
        json={
            "contents": "y",
            "target_path": "${HOME}/.config/${TENANT_ID}/y",
        },
    )
    assert resp.status_code == 200, resp.text


def test_set_sandbox_file_allows_name_matching_template_var(
    tmp_path: Path,
) -> None:
    """File names live in their own namespace — a Variable named
    ``GCP_CRED`` and a Sandbox file named ``GCP_CRED`` are independent.
    File names don't participate in ``${...}`` substitution.
    """
    client = make_test_client(tmp_path)
    create_var = client.put(
        "/api/admin/upstreams/gcp/template-vars/GCP_CRED",
        json={"value": "ghp_x", "is_secret": True},
    )
    assert create_var.status_code == 200, create_var.text
    resp = client.put(
        "/api/admin/upstreams/gcp/sandbox-files/GCP_CRED",
        json={"contents": "x", "target_path": "${HOME}/x"},
    )
    assert resp.status_code == 200, resp.text


def test_set_template_var_allows_name_matching_sandbox_file(
    tmp_path: Path,
) -> None:
    """Reverse direction of the previous test: creating a Variable
    with a name already used by a Sandbox file succeeds."""
    client = make_test_client(tmp_path)
    create_file = client.put(
        "/api/admin/upstreams/gcp/sandbox-files/GCP_CRED",
        json={"contents": "x", "target_path": "${HOME}/x"},
    )
    assert create_file.status_code == 200, create_file.text
    resp = client.put(
        "/api/admin/upstreams/gcp/template-vars/GCP_CRED",
        json={"value": "ghp_x", "is_secret": True},
    )
    assert resp.status_code == 200, resp.text


def test_set_template_var_rejects_system_variable_name(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/gcp/template-vars/HOME",
        json={"value": "ghp_x", "is_secret": True},
    )
    assert resp.status_code == 400
    assert "system variable" in resp.text.lower()


# --- delete_sandbox_file ---


def test_delete_sandbox_file_removes_row(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    client.put(
        "/api/admin/upstreams/gcp/sandbox-files/X",
        json={"contents": "y", "target_path": "${HOME}/x"},
    )
    resp = client.delete("/api/admin/upstreams/gcp/sandbox-files/X")
    assert resp.status_code == 200
    listing = client.get("/api/admin/upstreams/gcp/sandbox-files")
    assert listing.json() == []


def test_delete_sandbox_file_missing_is_idempotent(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.delete("/api/admin/upstreams/gcp/sandbox-files/MISSING")
    assert resp.status_code == 200


# --- system_variables ---


def test_list_system_variables_returns_home_for_stdio(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/system-variables?transport=stdio")
    assert resp.status_code == 200
    rows = resp.json()
    names = [r["name"] for r in rows]
    assert "HOME" in names
    home_row = next(r for r in rows if r["name"] == "HOME")
    assert home_row["value"].startswith("/home")


def test_list_system_variables_empty_for_http(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get(
        "/api/admin/system-variables?transport=streamable_http",
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_system_variables_rejects_unknown_transport(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/system-variables?transport=bogus")
    # FastAPI's enum validation surfaces a 422 for unknown values.
    assert resp.status_code == 422
