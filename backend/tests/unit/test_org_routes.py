"""Tests for org REST routes + org-context middleware (Phase 3)."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as


CONFIG_JSON = {
    "roles": {
        "admin": {
            "is_admin": True,
            "settings": {"mcp_access": {"auto_enable_new": True}},
        },
        "user": {
            "is_default": True,
            "settings": {},
        },
    },
    "users": {
        "admin@example.com": {"role": "admin"},
    },
}

def make_test_client(tmp_path: Path) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG_JSON))
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
        server_url="http://localhost:8000"
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager"
        ".UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry"
        ".ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    login_as(client, "admin@example.com")
    return client


# --- Features endpoint ---


def test_features_endpoint_returns_mode(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/config/features")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "standalone"
    assert "allow_stdio_mcp" in data


# --- Org creation (standalone = rejected) ---


def test_create_org_rejected_in_standalone(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/orgs",
        json={"slug": "neworg", "display_name": "New Org"}
    )
    assert resp.status_code == 400
    assert "standalone" in resp.json()["detail"].lower()


# --- List orgs (standalone) ---


def test_list_orgs_standalone(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/orgs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["orgs"]) == 1
    assert data["orgs"][0]["slug"] == "default"


# --- /api/auth/me returns orgs in standalone ---


def test_me_returns_orgs_standalone(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "admin@example.com"
    assert len(data["orgs"]) == 1
    assert data["orgs"][0]["slug"] == "default"
    assert data["orgs"][0]["role"] == "admin"
    # In standalone mode, current_org is always the default org
    assert data["current_org"] is not None
    assert data["current_org"]["slug"] == "default"


# --- Org context middleware (standalone mode) ---


def test_org_context_standalone_always_default(tmp_path: Path) -> None:
    """In standalone mode the org context middleware should set
    ``current_org_id`` to ``DEFAULT_ORG_ID``, and all existing
    endpoints should work as before (no org prefix needed)."""
    client = make_test_client(tmp_path)
    # Dashboard admin endpoints still work
    resp = client.get("/api/admin/upstreams")
    assert resp.status_code == 200


# --- Slug enumeration anti-pattern ---


def test_switch_org_rejects_nonexistent_slug(tmp_path: Path) -> None:
    """Switching to a slug that doesn't exist should return 401 (not 404)."""
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/orgs/nonexistent/switch"
    )
    assert resp.status_code == 401


# --- Org info endpoint ---


def test_org_info_returns_member_count(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/orgs/default/info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "default"
    assert data["display_name"] == "Default"
    assert isinstance(data["member_count"], int)


def test_org_info_rejects_nonexistent(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/orgs/nonexistent/info")
    assert resp.status_code == 401


# --- Delete org (standalone = rejected) ---


def test_delete_org_rejected_in_standalone(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.delete("/api/orgs/default")
    # Standalone mode doesn't support deletion
    assert resp.status_code in (400, 500)


def test_create_app_wires_every_org_scoped_repo_into_org_service(
    tmp_path: Path,
) -> None:
    """Guard against silent deletion leaks: ``create_app`` must inject
    EVERY org-scoped repo (and the runtime-teardown hook) into the
    OrgService, or that collection survives org deletion. The repos are
    optional kwargs on ``OrgService`` — a forgotten one still constructs
    fine, so only this assertion catches the omission. Keep this list in
    sync with the org-scoped fields on ``StorageBundle``."""
    client = make_test_client(tmp_path)
    org_service = client.app.state.org_service  # type: ignore[attr-defined]
    for attr in (
        "_service_token_repo",
        "_connection_repo",
        "_upstream_config_repo",
        "_tool_catalog_repo",
        "_sandbox_persistence_repo",
        "_template_var_repo",
        "_sandbox_file_repo",
        "_audit_repo",
    ):
        assert getattr(org_service, attr) is not None, (
            f"create_app did not wire OrgService.{attr} — that collection "
            f"would leak on org deletion"
        )
    # The teardown hook (runtime stop + slug-cache invalidation) is
    # late-bound via set_runtime_teardown; it must be wired too.
    assert org_service._runtime_teardown is not None


# --- Admin gate uses is_admin flag, not the literal role name "admin" ---


CONFIG_JSON_OPERATOR_ADMIN: dict[str, Any] = {
    "roles": {
        # Admin flag lives on a role *not* named "admin".
        "operator": {
            "is_admin": True,
            "settings": {"mcp_access": {"auto_enable_new": True}},
        },
        # A second role to make sure the test doesn't trivially pass
        # because "operator" is the only role.
        "user": {"is_default": True, "settings": {}},
    },
    "users": {
        "operator@example.com": {"role": "operator"},
    },
}


def make_test_client_with_config(
    tmp_path: Path, config_obj: Mapping[str, Any], login_email: str,
) -> TestClient:
    """Same wiring as ``make_test_client`` but with a caller-supplied
    config + login email — used to test gates that should respect
    ``is_admin`` rather than the literal role name ``"admin"``."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_obj))
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
        "mcpolis.adapters.upstream_clients.client_manager"
        ".UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry"
        ".ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    login_as(client, login_email)
    return client


def test_delete_org_admin_gate_uses_is_admin_not_role_name(
    tmp_path: Path,
) -> None:
    """Phase 0 red test for the "any role can be admin" refactor.

    A user assigned to a role flagged ``is_admin=True`` must be
    treated as admin even when the role's *name* is something
    other than ``"admin"``. The delete-org gate at
    [org_routes.py:297](backend/src/mcpolis/entrypoints/routes/org_routes.py#L297)
    currently reads ``role != "admin"`` against the membership-row
    role string returned by ``OrgService.get_user_role`` — so a
    user whose role is ``"operator"`` (with ``is_admin=True``) is
    rejected with 403 instead of being allowed through to the
    actual delete (which 400s in standalone mode).

    After the refactor, this user should NOT see a 403; they
    should reach the underlying delete and see whatever status
    the repo returns (400/500 in standalone). Today: 403. **Fails.**
    """
    client = make_test_client_with_config(
        tmp_path, CONFIG_JSON_OPERATOR_ADMIN, "operator@example.com",
    )
    resp = client.delete("/api/orgs/default")
    assert resp.status_code != 403, (
        "operator@example.com is admin via is_admin=True but the "
        "literal 'role != \"admin\"' gate is rejecting them"
    )


def test_create_org_summary_returns_is_admin_flag(tmp_path: Path) -> None:
    """Phase 0 red test: ``GET /api/orgs`` must surface ``is_admin``
    on each row so the frontend can stop string-comparing the role
    name. Today the field doesn't exist on ``OrgSummary``, so the
    response is missing it."""
    client = make_test_client_with_config(
        tmp_path, CONFIG_JSON_OPERATOR_ADMIN, "operator@example.com",
    )
    resp = client.get("/api/orgs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["orgs"]) == 1
    assert data["orgs"][0]["is_admin"] is True, (
        "OrgSummary should expose is_admin computed from the role's "
        "is_admin flag — operator@example.com is admin"
    )


