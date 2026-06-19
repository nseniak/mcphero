"""Tests for dashboard REST API endpoints."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as


def make_import_confirm_body(
    servers: dict[str, dict[str, Any]],
    targets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a standard-scope ``import/confirm`` body from a flat
    ``{id: server_config}`` map. ``targets`` overrides the created id per
    original id (default: target id == original id)."""
    target_map = targets or {}
    return {
        "data": {"mcpServers": servers},
        "entries": [
            {
                "scope": "standard",
                "project_path": None,
                "original_id": oid,
                "target_id": target_map.get(oid, oid),
            }
            for oid in servers
        ],
    }

MCP_JSON = json.dumps({
    "mcpServers": {
        "github": {"url": "http://localhost:9000/mcp"},
        "mixpanel": {"url": "http://localhost:9001/mcp"},
    }
})

CONFIG_JSON = json.dumps({
    "upstreams": {
        "github": {"display_name": "GitHub", "auth_mode": "service_account"},
        "mixpanel": {"display_name": "Mixpanel", "auth_mode": "per_user_oauth"},
    },
    "roles": {
        "admin": {
            "is_admin": True,
            "settings": {
                "mcp_access": {"mcps": {"github": True, "mixpanel": True}},
            },
        },
        "developer": {
            "settings": {
                "mcp_access": {"mcps": {"github": True}},
            },
        },
    },
    "users": {
        "admin@example.com": {"role": "admin"},
        "dev@example.com": {"role": "developer"},
    },
})


def make_test_client(
    tmp_path: Path,
    *,
    login: str | None = "admin@example.com",
    plan: str = "team",
) -> TestClient:
    """Build a TestClient backed by the dev-stub provider and (by
    default) log in as the admin user.

    Pass ``login=None`` to get an anonymous client (e.g. for testing
    the unauthenticated paths). Pass a different email to log in as a
    different user — calling :func:`login_as` on the returned client
    works the same way mid-test.

    ``plan`` controls the standalone org's subscription plan. Defaults
    to ``team`` so existing tests aren't gated by the Free-tier
    limits introduced alongside the plan-mechanics rollout. Tests that
    want to assert Free-plan behaviour pass ``plan="free"``.
    """
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(MCP_JSON)
    config = tmp_path / "config.json"
    config.write_text(CONFIG_JSON)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "subscription.json").write_text(
        json.dumps({"plan": plan}),
    )
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
        server_url="http://localhost:8000")
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


# --- Admin endpoints ---


def test_admin_list_upstreams(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    ids = {u["id"] for u in data}
    assert ids == {"github", "mixpanel"}
    github = next(u for u in data if u["id"] == "github")
    assert github["auth_mode"] == "service_account"
    assert github["tool_count"] == 0


def test_admin_get_upstream(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/github")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "github"
    assert data["url"] == "http://localhost:9000/mcp"
    assert data["auth_mode"] == "service_account"
    # Detail page button + status pill drive the cross-tab "Starting…"
    # pill off this field. Pinned because the frontend's previous
    # ``sandbox_state`` pipeline (deleted in Phase 5 / runner removal)
    # left ``UpstreamSummary.starting`` as dead data on the wire for
    # the listing AND no equivalent on detail at all — fixed by
    # adding ``starting`` to UpstreamDetail and populating it from
    # ``client_manager.is_starting``. False at rest.
    assert data["starting"] is False


def test_admin_get_upstream_not_found(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/nope")
    assert resp.status_code == 404


def test_admin_list_tools_empty(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/tools")
    assert resp.status_code == 200
    assert resp.json() == []


def test_admin_audit_empty(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["entries"] == []
    assert data["count"] == 0


def test_admin_audit_with_entries(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    audit_path = tmp_path / "data" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"user_id": "alice", "upstream_id": "github",
         "tool": "create_issue", "policy_decision": "allowed"},
        {"user_id": "bob", "upstream_id": "mixpanel",
         "tool": "query", "policy_decision": "allowed"},
        {"user_id": "alice", "upstream_id": "github",
         "tool": "list_repos", "policy_decision": "denied"},
    ]
    with audit_path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    resp = client.get("/api/admin/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3

    resp = client.get(
        "/api/admin/audit?user_id=alice")
    data = resp.json()
    assert data["count"] == 2

    resp = client.get(
        "/api/admin/audit?mcp_id=mixpanel")
    data = resp.json()
    assert data["count"] == 1
    assert data["entries"][0]["user_id"] == "bob"


def test_admin_list_users(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/users")
    assert resp.status_code == 200
    data = resp.json()
    emails = {u["email"] for u in data}
    assert "admin@example.com" in emails
    assert "dev@example.com" in emails
    admin_user = next(u for u in data if u["email"] == "admin@example.com")
    assert admin_user["role"] == "admin"
    assert admin_user["is_admin"] is True


def test_admin_list_roles(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/roles")
    assert resp.status_code == 200
    data = resp.json()
    names = {r["name"] for r in data}
    assert "admin" in names
    assert "developer" in names


# --- Write endpoints ---


def test_admin_add_and_remove_user(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    # Add a user
    resp = client.post(
        "/api/admin/users",
        json={"email": "new@example.com", "role": "developer"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "new@example.com"
    assert data["role"] == "developer"

    # Verify they appear in the list
    resp = client.get("/api/admin/users")
    emails = {u["email"] for u in resp.json()}
    assert "new@example.com" in emails

    # Remove them
    resp = client.delete("/api/admin/users/new@example.com")
    assert resp.status_code == 200

    # Verify they're gone
    resp = client.get("/api/admin/users")
    emails = {u["email"] for u in resp.json()}
    assert "new@example.com" not in emails


def _read_connections(tmp_path: Path) -> dict[str, object]:
    """Read the file-store's persisted connections.json directly.
    Lets a test assert on the ``enabled:<id>`` keys without going
    through the API surface — the keys are what survives a restart.
    """
    data_path = tmp_path / "data" / "connections.json"
    if not data_path.exists():
        return {}
    return cast(dict[str, object], json.loads(data_path.read_text()))


def test_admin_add_upstream(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "new-mcp",
            "display_name": "New MCP",
            "url": "http://localhost:9999/mcp",
            "auth_mode": "service_account",
        })
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == "new-mcp"

    # Newly added upstreams must persist ``enabled: False`` so the
    # next restart's reconciler skips them — the user's
    # ``was_ready`` rule. ``clear_enabled`` (which removed the key
    # entirely and silently fell back to default-enabled) shipped
    # against this comment for a long time.
    connections = _read_connections(tmp_path)
    assert connections.get("enabled:new-mcp") is False, (
        f"expected enabled:new-mcp = False after add_upstream, "
        f"got {connections.get('enabled:new-mcp')!r}"
    )

    # Verify it appears
    resp = client.get("/api/admin/upstreams")
    ids = {u["id"] for u in resp.json()}
    assert "new-mcp" in ids

    # Verify all roles get new-mcp enabled
    resp = client.get("/api/admin/roles/access")
    access_data: list[dict[str, object]] = resp.json()
    by_name = {str(r["name"]): r for r in access_data}

    def role_mcps(name: str) -> dict[str, object]:
        mcp_access = by_name[name].get("mcp_access")
        if not isinstance(mcp_access, dict):
            return {}
        m = cast(dict[str, object], mcp_access).get("mcps")
        if not isinstance(m, dict):
            return {}
        return cast(dict[str, object], m)

    # Both roles have auto_enable_new=False → new-mcp is disabled
    assert role_mcps("admin").get("new-mcp") is False
    assert role_mcps("developer").get("new-mcp") is False

    # Remove it
    resp = client.delete("/api/admin/upstreams/new-mcp")
    assert resp.status_code == 200


def test_admin_disconnect_persists_disabled_state(tmp_path: Path) -> None:
    """Admin Stop / Disconnect must persist ``enabled: False`` so the
    boot reconciler skips this upstream on the next restart.
    Headline UX claim of the ``was_ready`` rule — the bug it fixes
    was that ``clear_enabled`` removed the marker (default-enabled),
    so a Stopped upstream silently auto-reconnected on every deploy.
    """
    client = make_test_client(tmp_path)

    # github is service_account, mixpanel is per_user_oauth — both
    # go through the same disconnect route + connection-store flow.
    resp = client.post("/api/admin/upstreams/github/disconnect")
    assert resp.status_code == 200, resp.text

    connections = _read_connections(tmp_path)
    assert connections.get("enabled:github") is False, (
        f"expected enabled:github = False after disconnect, "
        f"got {connections.get('enabled:github')!r}"
    )


def test_admin_remove_upstream_clears_enabled_marker(tmp_path: Path) -> None:
    """Removing an upstream must take its ``enabled:<id>`` marker
    with it. Under bistate (Phase E) ``set_enabled`` deletes the
    explicit-disabled row; if a re-add of the same id ever happens
    later, the new ``add_upstream`` flow re-writes ``set_disabled``
    cleanly.
    """
    client = make_test_client(tmp_path)

    # Add an upstream → connections.json should have enabled:tmp = False.
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "tmp",
            "display_name": "Tmp",
            "url": "http://localhost:9999/mcp",
            "auth_mode": "service_account",
        })
    assert resp.status_code == 201
    assert _read_connections(tmp_path).get("enabled:tmp") is False

    # Remove → bistate set_enabled deletes the marker entirely.
    resp = client.delete("/api/admin/upstreams/tmp")
    assert resp.status_code == 200
    connections = _read_connections(tmp_path)
    assert "enabled:tmp" not in connections, (
        f"expected enabled:tmp to be removed after delete; "
        f"got {connections.get('enabled:tmp')!r}"
    )


def test_admin_remove_upstream_404_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.delete("/api/admin/upstreams/never-existed")
    assert resp.status_code == 404


def test_admin_remove_upstream_swallows_sandbox_cleanup_failure(
    tmp_path: Path,
) -> None:
    """Provider-side cleanup runs after config removal and is wrapped
    in a try/except so a transient sandbox SDK error can't block the
    operator's delete. If we patch ``cleanup_sandbox_state_for_upstream``
    to raise, the route must still return 200 and still remove the
    enabled marker — the reconciler is the eventual-consistency net
    documented in ``cleanup_sandbox_state_for_upstream``'s docstring.
    """
    client = make_test_client(tmp_path)

    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "tmp",
            "display_name": "Tmp",
            "url": "http://localhost:9999/mcp",
            "auth_mode": "service_account",
        })
    assert resp.status_code == 201

    with patch(
        "mcpolis.adapters.upstream_clients.client_manager"
        ".UpstreamClientManager.cleanup_sandbox_state_for_upstream",
        side_effect=RuntimeError("provider blew up"),
    ):
        resp = client.delete("/api/admin/upstreams/tmp")

    assert resp.status_code == 200
    assert "enabled:tmp" not in _read_connections(tmp_path)


def test_admin_reconnect_404_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/upstreams/never-existed/reconnect")
    assert resp.status_code == 404


def test_admin_reconnect_service_account_clears_disabled_marker(
    tmp_path: Path,
) -> None:
    """Reconnect on a service_account upstream must first remove any
    explicit-disabled marker so the boot reconciler picks the upstream
    up next restart. Pre-Phase-E this site wrote ``enabled: True``;
    under bistate it deletes the marker — same is_enabled outcome,
    but the prod ``connections`` collection no longer accumulates
    ``enabled: True`` rows that have no semantic meaning.
    """
    client = make_test_client(tmp_path)

    # Put github into an explicit-disabled state via the disconnect
    # route (service_account upstreams hit ``set_disabled`` there).
    resp = client.post("/api/admin/upstreams/github/disconnect")
    assert resp.status_code == 200
    assert _read_connections(tmp_path).get("enabled:github") is False

    # Reconnect — patch the fire-and-forget background connect so
    # the test doesn't hang on a real TCP attempt.
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager"
        ".UpstreamClientManager.connect_upstream",
    ):
        resp = client.post("/api/admin/upstreams/github/reconnect")

    assert resp.status_code == 200, resp.text
    connections = _read_connections(tmp_path)
    assert "enabled:github" not in connections, (
        f"expected enabled:github marker removed after reconnect; "
        f"got {connections.get('enabled:github')!r}"
    )


# Boundaries the refresh-endpoint tests mock. The refresh itself is
# kicked off via ``refresh_tools_in_background`` (``_REFRESH_BG`` below).
_IS_CONNECTED = (
    "mcpolis.adapters.upstream_clients.client_manager"
    ".UpstreamClientManager.is_connected"
)
_DISCONNECT = (
    "mcpolis.adapters.upstream_clients.client_manager"
    ".UpstreamClientManager.disconnect_upstream"
)
_READINESS = (
    "mcpolis.entrypoints.routes.dashboard.upstream_admin"
    ".resolve_upstream_readiness"
)
# The refresh endpoint is non-blocking: it kicks off the acquire+refresh
# in a background task (so an E2B-pause stall can't blow the request
# budget — the 2026-06-18 incident) and returns immediately. Endpoint
# tests patch this and invoke the captured on_success/on_error callbacks
# to exercise the outcome glue deterministically (no bg-task timing); the
# refresh logic itself is covered in test_refresh_tools_in_background.py.
_REFRESH_BG = (
    "mcpolis.entrypoints.routes.dashboard.upstream_admin"
    ".refresh_tools_in_background"
)


def test_admin_refresh_tools_404_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/upstreams/never-existed/refresh-tools")
    assert resp.status_code == 404


def test_admin_refresh_tools_409_when_not_active(tmp_path: Path) -> None:
    """The refresh button is gated on the MCP being active, and the
    endpoint must never connect: an inactive upstream is refused with
    409 and no session acquisition is attempted. ``start_all`` is
    patched out in make_test_client, so github has no live session.
    """
    client = make_test_client(tmp_path)
    with patch(_REFRESH_BG) as refresh_bg:
        resp = client.post("/api/admin/upstreams/github/refresh-tools")
    assert resp.status_code == 409, resp.text
    refresh_bg.assert_not_called()


def test_admin_refresh_tools_success(tmp_path: Path) -> None:
    """An active upstream returns immediately (refresh runs in the
    background) and the success callback clears any prior error. The
    endpoint is non-blocking so an E2B-pause stall can't time out the
    click (2026-06-18 incident); the actual acquire+refresh is covered in
    test_refresh_tools_in_background.py."""
    import asyncio

    client = make_test_client(tmp_path)
    with patch(_IS_CONNECTED, return_value=True), patch(
        _DISCONNECT, new_callable=AsyncMock,
    ) as disconnect, patch(_REFRESH_BG) as refresh_bg:
        resp = client.post("/api/admin/upstreams/github/refresh-tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is True
    assert body["upstream_id"] == "github"
    refresh_bg.assert_called_once()
    disconnect.assert_not_called()
    # The captured success callback must clear any prior connection error.
    asyncio.run(refresh_bg.call_args.kwargs["on_success"]())
    assert "error:github" not in _read_connections(tmp_path)


def test_admin_refresh_tools_failure_records_error_no_disconnect(
    tmp_path: Path,
) -> None:
    """A background refresh failure records the reason (error banner +
    popup) without tearing the session down — killing a warm sandbox or
    signing an OAuth admin out over a transient error would be worse. The
    endpoint returns 200 immediately; the error surfaces via the on_error
    callback + connection-error state, not the HTTP response."""
    import asyncio

    client = make_test_client(tmp_path)
    with patch(_IS_CONNECTED, return_value=True), patch(
        _DISCONNECT, new_callable=AsyncMock,
    ) as disconnect, patch(_REFRESH_BG) as refresh_bg:
        resp = client.post("/api/admin/upstreams/github/refresh-tools")
    assert resp.status_code == 200, resp.text
    assert resp.json()["connected"] is True
    # Drive the captured error callback as the background task would.
    asyncio.run(refresh_bg.call_args.kwargs["on_error"]("list_tools blew up"))
    disconnect.assert_not_called()
    error_entry = _read_connections(tmp_path).get("error:github")
    assert isinstance(error_entry, dict)
    assert "list_tools blew up" in error_entry["error"]


def test_admin_refresh_tools_oauth_not_ready_409(tmp_path: Path) -> None:
    """An OAuth upstream with no admin token is Not Ready, so the
    button is disabled and the endpoint refuses with 409 (regression:
    the guard must use readiness, not shared-session is_connected —
    OAuth upstreams have no shared session and were spuriously 409'd
    even when ready)."""
    client = make_test_client(tmp_path)
    # mixpanel is per_user_oauth with no stored token in a fresh client.
    resp = client.post("/api/admin/upstreams/mixpanel/refresh-tools")
    assert resp.status_code == 409, resp.text


def test_admin_refresh_tools_oauth_ready_success(tmp_path: Path) -> None:
    """A ready OAuth upstream returns immediately and kicks off the
    background refresh (effective user = the slot owner)."""
    client = make_test_client(tmp_path)
    with patch(
        _READINESS,
        new_callable=AsyncMock,
        return_value=(True, "admin@example.com"),
    ), patch(_REFRESH_BG) as refresh_bg:
        resp = client.post("/api/admin/upstreams/mixpanel/refresh-tools")
    assert resp.status_code == 200, resp.text
    assert resp.json()["connected"] is True
    refresh_bg.assert_called_once()
    assert (
        refresh_bg.call_args.kwargs["effective_user"] == "admin@example.com"
    )


def test_admin_connect_rejects_service_account(tmp_path: Path) -> None:
    """The ``/connect`` route is OAuth-only; service_account upstreams
    must be 400. Reconnect / disconnect are the verbs that work for
    them. Pinned because the route's first check is auth-mode and
    a future refactor that loosens it would silently let
    service_account go through the OAuth code path."""
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/upstreams/github/connect")
    assert resp.status_code == 400


def test_admin_import_confirm_persists_disabled_for_added(
    tmp_path: Path,
) -> None:
    """Bulk-imported upstreams must each get ``enabled: False`` so the
    boot reconciler skips them until an admin clicks Connect — same
    off-by-default intent as ``add_upstream``. Bulk import previously
    used ``clear_enabled`` which silently fell back to default-
    enabled; this test pins the corrected behavior."""
    client = make_test_client(tmp_path)

    resp = client.post(
        "/api/admin/upstreams/import/confirm",
        json=make_import_confirm_body({
            "imp-a": {"url": "http://localhost:9001/mcp"},
            "imp-b": {"url": "http://localhost:9002/mcp"},
        }))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["added"]) == ["imp-a", "imp-b"]
    assert body["skipped"] == []
    assert body["errors"] == []

    connections = _read_connections(tmp_path)
    assert connections.get("enabled:imp-a") is False
    assert connections.get("enabled:imp-b") is False


def test_admin_import_confirm_errors_on_existing_id(tmp_path: Path) -> None:
    """Targeting an existing id is a row error, not a silent skip: the
    operator picks the final id inline, so a collision with a live
    upstream is something they must resolve, not something we paper
    over by clobbering the existing one."""
    client = make_test_client(tmp_path)

    # github already exists in MCP_JSON / CONFIG_JSON.
    resp = client.post(
        "/api/admin/upstreams/import/confirm",
        json=make_import_confirm_body(
            {"github": {"url": "http://localhost:9999/mcp"}},
        ))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == []
    error_ids = [e["id"] for e in body["errors"]]
    assert error_ids == ["github"]
    assert "already exists" in body["errors"][0]["error"]


def test_admin_import_confirm_blocked_by_http_plan_cap(
    tmp_path: Path,
) -> None:
    """Bulk import must enforce the same per-transport cap as the
    single-MCP add path. Free plan caps remote-HTTP at 5; the fixture
    seeds 2 (github, mixpanel), so importing 4 new HTTP entries would
    push the total to 6. The pre-loop gate fires 402 and no entries
    are written — bulk import is atomic, not partial."""
    client = make_test_client(tmp_path, plan="free")

    resp = client.post(
        "/api/admin/upstreams/import/confirm",
        json=make_import_confirm_body({
            "imp-a": {"url": "http://localhost:9001/mcp"},
            "imp-b": {"url": "http://localhost:9002/mcp"},
            "imp-c": {"url": "http://localhost:9003/mcp"},
            "imp-d": {"url": "http://localhost:9004/mcp"},
        }))
    assert resp.status_code == 402, resp.text

    listing = client.get("/api/admin/upstreams")
    assert listing.status_code == 200
    ids = {u["id"] for u in listing.json()}
    assert ids == {"github", "mixpanel"}


def test_admin_import_confirm_blocked_by_stdio_plan_cap(
    tmp_path: Path,
) -> None:
    """Free plan caps hosted-stdio at 1. Importing 2 stdio entries on
    a fresh org (0 stdio existing) trips the gate on the second entry
    in the pre-loop pass and the whole import is rejected with 402."""
    client = make_test_client(tmp_path, plan="free")

    resp = client.post(
        "/api/admin/upstreams/import/confirm",
        json=make_import_confirm_body({
            "stdio-a": {"command": "echo", "args": ["a"]},
            "stdio-b": {"command": "echo", "args": ["b"]},
        }))
    assert resp.status_code == 402, resp.text

    listing = client.get("/api/admin/upstreams")
    assert listing.status_code == 200
    ids = {u["id"] for u in listing.json()}
    assert ids == {"github", "mixpanel"}


def test_admin_import_confirm_claude_json_creates_under_target_ids(
    tmp_path: Path,
) -> None:
    """A ``.claude.json`` import creates one upstream per selected row
    under the operator's chosen (project-suffixed) target id, resolving
    each raw config from the original blob by scope + project path."""
    client = make_test_client(tmp_path)

    data = {
        "projects": {
            "/home/me/web": {
                "mcpServers": {"github": {"url": "http://localhost:9101/mcp"}},
            },
            "/home/me/api": {
                "mcpServers": {"github": {"url": "http://localhost:9102/mcp"}},
            },
        },
    }
    resp = client.post(
        "/api/admin/upstreams/import/confirm",
        json={
            "data": data,
            "entries": [
                {
                    "scope": "project",
                    "project_path": "/home/me/web",
                    "original_id": "github",
                    "target_id": "web-github",
                },
                {
                    "scope": "project",
                    "project_path": "/home/me/api",
                    "original_id": "github",
                    "target_id": "api-github",
                },
            ],
        })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["added"]) == ["api-github", "web-github"]
    assert body["errors"] == []

    detail = client.get("/api/admin/upstreams/web-github").json()
    assert detail["url"] == "http://localhost:9101/mcp"


def test_admin_import_confirm_errors_on_duplicate_target(
    tmp_path: Path,
) -> None:
    """Two selected rows targeting the same id: the first is created,
    the second is a row error so a scripted caller can't smuggle a
    collision past the dialog's inline validation."""
    client = make_test_client(tmp_path)

    resp = client.post(
        "/api/admin/upstreams/import/confirm",
        json={
            "data": {
                "projects": {
                    "/p/web": {
                        "mcpServers": {"github": {"url": "http://localhost:9201/mcp"}},
                    },
                    "/p/api": {
                        "mcpServers": {"github": {"url": "http://localhost:9202/mcp"}},
                    },
                },
            },
            "entries": [
                {
                    "scope": "project", "project_path": "/p/web",
                    "original_id": "github", "target_id": "dupe",
                },
                {
                    "scope": "project", "project_path": "/p/api",
                    "original_id": "github", "target_id": "dupe",
                },
            ],
        })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == ["dupe"]
    assert [e["id"] for e in body["errors"]] == ["dupe"]
    assert "Duplicate id" in body["errors"][0]["error"]


@pytest.mark.asyncio
async def test_admin_disconnect_survives_restart(tmp_path: Path) -> None:
    """End-to-end: Stop → simulated restart → upstream stays
    skipped. Pins the user-facing claim that "Stop survives a
    restart" — the bug-hiding-in-plain-sight before
    ``set_disabled`` was that the marker was removed and default-
    enabled silently re-engaged on next boot.

    Restart simulation: instantiate a fresh ``FileConnectionStore``
    against the same on-disk file the dashboard wrote to. The boot
    reconciler reads ``get_disabled_ids`` from this exact shape on
    next boot — if Stop survives, github appears in the result.
    """
    from mcpolis.adapters.repositories.file_connection_store import (
        FileConnectionStore,
    )
    client = make_test_client(tmp_path)

    resp = client.post("/api/admin/upstreams/github/disconnect")
    assert resp.status_code == 200, resp.text

    fresh_store = FileConnectionStore(tmp_path / "data")
    disabled = await fresh_store.get_disabled_ids("default")
    assert "github" in disabled, (
        f"github should appear in disabled_ids after Disconnect — "
        f"the boot reconciler will skip it. got disabled={disabled!r}"
    )


def test_admin_add_stdio_upstream_with_resources(tmp_path: Path) -> None:
    """Stdio create should persist resource fields when supplied.

    Runs on the default Team client (unrestricted combos) and picks a
    non-default combo (1 vCPU / 2048 MiB) so the round-trip proves the
    supplied fields aren't silently dropped back to the model default
    (1 vCPU / 1024 MiB). The plan-combo gate is a no-op on Team.
    """
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "stdio-with-resources",
            "display_name": "Stdio With Resources",
            "command": "echo",
            "args": ["hello"],
            "auth_mode": "service_account",
            "cpu_vcpus": 1.0,
            "memory_mb": 2048,
        })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"] == "stdio-with-resources"

    detail = client.get("/api/admin/upstreams/stdio-with-resources")
    assert detail.status_code == 200
    sandbox = detail.json()["sandbox_resources"]
    assert sandbox is not None
    assert sandbox["cpu_vcpus"] == 1.0
    assert sandbox["memory_mb"] == 2048


def test_admin_add_stdio_upstream_off_grid_rejected(tmp_path: Path) -> None:
    """Off-grid resource values must 400 with a structured detail.

    The frontend uses the ``field`` key to flag the offending control
    in the create form, so the contract has to match the PUT endpoint.
    """
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "stdio-off-grid",
            "display_name": "Stdio Off Grid",
            "command": "echo",
            "auth_mode": "service_account",
            "cpu_vcpus": 3.0,
        })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["field"] == "cpu_vcpus"
    assert detail["value"] == "3.0"


# --- update_upstream extended with sandbox_resources patch ---


def test_update_upstream_sandbox_resources_applies_patch(tmp_path: Path) -> None:
    """The detail-page edit form folds the resource picker into the
    SETTINGS Save flow via a ``sandbox_resources`` patch — same
    pattern as ``template_var_changes``."""
    client = make_test_client(tmp_path)
    client.post(
        "/api/admin/upstreams",
        json={
            "id": "stdio-rsc",
            "display_name": "Stdio Rsc",
            "command": "echo",
            "auth_mode": "service_account",
        },
    )
    resp = client.put(
        "/api/admin/upstreams/stdio-rsc",
        json={
            "sandbox_resources": {
                "cpu_vcpus": 1.0,
                "memory_mb": 2048,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    detail = client.get("/api/admin/upstreams/stdio-rsc").json()
    assert detail["sandbox_resources"]["cpu_vcpus"] == 1.0
    assert detail["sandbox_resources"]["memory_mb"] == 2048


def test_update_upstream_sandbox_resources_off_grid_rejected(
    tmp_path: Path,
) -> None:
    """Off-grid value via the patch must 400 with the structured
    detail the admin UI uses to flag the offending control."""
    client = make_test_client(tmp_path)
    client.post(
        "/api/admin/upstreams",
        json={
            "id": "stdio-bad",
            "display_name": "Stdio Bad",
            "command": "echo",
            "auth_mode": "service_account",
        },
    )
    resp = client.put(
        "/api/admin/upstreams/stdio-bad",
        json={"sandbox_resources": {"cpu_vcpus": 3.0}},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["field"] == "cpu_vcpus"
    assert detail["value"] == "3.0"


def test_update_upstream_sandbox_resources_rejects_http_upstream(
    tmp_path: Path,
) -> None:
    """``sandbox_resources`` is stdio-only; sending it for an HTTP
    upstream must 400 cleanly rather than silently no-op."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github",
        json={"sandbox_resources": {"cpu_vcpus": 2.0}},
    )
    assert resp.status_code == 400


def test_update_upstream_server_config_preserves_existing_resources(
    tmp_path: Path,
) -> None:
    """Editing the JSON config must NOT silently reset the resource
    fields that the operator tuned via the picker. The admin's JSON
    editor only round-trips ``command/args/env`` — the backend has
    to carry CPU/RAM/disk forward from the existing stdio config."""
    client = make_test_client(tmp_path)
    client.post(
        "/api/admin/upstreams",
        json={
            "id": "stdio-preserve",
            "display_name": "Stdio Preserve",
            "command": "echo",
            "auth_mode": "service_account",
            "cpu_vcpus": 1.0,
            "memory_mb": 2048,
        },
    )
    # Edit the JSON config without including resource fields (the
    # admin UI's editor never does).
    resp = client.put(
        "/api/admin/upstreams/stdio-preserve",
        json={
            "server_config": {
                "command": "echo",
                "args": ["new-arg"],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    detail = client.get("/api/admin/upstreams/stdio-preserve").json()
    assert detail["sandbox_resources"]["cpu_vcpus"] == 1.0
    assert detail["sandbox_resources"]["memory_mb"] == 2048
    assert detail["server_config"]["args"] == ["new-arg"]


def test_set_sandbox_resources_endpoint_removed(tmp_path: Path) -> None:
    """The legacy ``PUT /api/admin/upstreams/{id}/sandbox/resources``
    endpoint is gone — the unified PUT is the only path now."""
    client = make_test_client(tmp_path)
    client.post(
        "/api/admin/upstreams",
        json={
            "id": "stdio-old",
            "display_name": "Stdio Old",
            "command": "echo",
            "auth_mode": "service_account",
        },
    )
    resp = client.put(
        "/api/admin/upstreams/stdio-old/sandbox/resources",
        json={"cpu_vcpus": 2.0},
    )
    assert resp.status_code == 404


def test_admin_add_stdio_upstream_defaults(tmp_path: Path) -> None:
    """Stdio create without resource fields should persist model
    defaults (1 vCPU / 1024 MiB / 0 disk).

    Guards against regressions where a future "make it required" change
    would silently break existing JSON imports / API clients.
    """
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "stdio-defaults",
            "display_name": "Stdio Defaults",
            "command": "echo",
            "auth_mode": "service_account",
        })
    assert resp.status_code == 201, resp.text

    detail = client.get("/api/admin/upstreams/stdio-defaults")
    assert detail.status_code == 200
    sandbox = detail.json()["sandbox_resources"]
    assert sandbox is not None
    assert sandbox["cpu_vcpus"] == 1.0
    assert sandbox["memory_mb"] == 1024
    assert sandbox["disk_gb"] == 0


# --- User endpoints ---


def test_user_mcps_dev_mode(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, login="dev@example.com")
    resp = client.get("/api/user/mcps")
    assert resp.status_code == 200
    data = resp.json()
    ids = {m["id"] for m in data}
    assert "github" in ids


def test_user_mcps_shows_connection_status(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/user/mcps")
    data = resp.json()
    mixpanel = next(m for m in data if m["id"] == "mixpanel")
    assert mixpanel["auth_mode"] == "per_user_oauth"
    assert mixpanel["user_connection_status"] == "not_connected"


# --- Auth me endpoint ---


def test_auth_me_dev_mode(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "admin@example.com"
    assert data["is_admin"] is True
    assert "admin" in data["roles"]


def test_auth_me_non_admin(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, login="dev@example.com")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "dev@example.com"
    assert data["is_admin"] is False
    assert "developer" in data["roles"]


def test_auth_logout(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204


# --- Argument constraints ---


def test_set_argument_constraint_allow(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/admin/upstreams/github/tools/query/constraints/sql",
        json={"pattern": r"^SELECT\s", "mode": "allow"})
    assert resp.status_code == 200
    data = resp.json()
    constraint = data["argument_constraints"]["github__query"]["sql"]
    assert constraint["pattern"] == r"^SELECT\s"
    assert constraint["mode"] == "allow"


def test_set_argument_constraint_forbid(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/admin/upstreams/github/tools/query/constraints/sql",
        json={"pattern": r"DROP|DELETE", "mode": "forbid"})
    assert resp.status_code == 200
    data = resp.json()
    constraint = data["argument_constraints"]["github__query"]["sql"]
    assert constraint["pattern"] == r"DROP|DELETE"
    assert constraint["mode"] == "forbid"


def test_set_argument_constraint_invalid_regex(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/admin/upstreams/github/tools/query/constraints/sql",
        json={"pattern": r"[invalid"})
    assert resp.status_code == 400
    assert "Invalid regex" in resp.json()["detail"]


def test_remove_argument_constraint(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    client.put(
        "/api/admin/roles/admin/upstreams/github/tools/query/constraints/sql",
        json={"pattern": r"^SELECT", "mode": "allow"})
    resp = client.delete(
        "/api/admin/roles/admin/upstreams/github/tools/query/constraints/sql")
    assert resp.status_code == 200
    data = resp.json()
    assert "github__query" not in data["argument_constraints"]


def test_set_argument_constraint_nonexistent_role(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/nonexistent/upstreams/github/tools/query/constraints/sql",
        json={"pattern": r"^SELECT", "mode": "allow"})
    assert resp.status_code == 404


# --- Test-mode MCP-token minting ---


def make_oauth_test_client(tmp_path: Path) -> TestClient:
    """Test client with OAuth enabled so the gateway provider is wired."""
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
        oauth_provider="google",
        google_client_id="test-google-client",
        google_client_secret="test-google-secret",
        session_secret="test-session-secret",
        server_url="http://localhost:8000")
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    return TestClient(app, raise_server_exceptions=True)


def test_test_mcp_token_not_registered_without_test_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCPOLIS_TEST_MODE", raising=False)
    client = make_oauth_test_client(tmp_path)
    resp = client.post(
        "/api/auth/test-mcp-token",
        json={"email": "admin@example.com", "org_slug": "default"})
    # Route isn't registered when test_mode is off — stronger than a
    # runtime 403 gate because a misconfig can't expose it.
    assert resp.status_code == 404


def test_test_mcp_token_returns_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPOLIS_TEST_MODE", "1")
    client = make_oauth_test_client(tmp_path)
    resp = client.post(
        "/api/auth/test-mcp-token",
        json={"email": "admin@example.com", "org_slug": "default"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    assert isinstance(body["access_token"], str) and len(body["access_token"]) > 10


@pytest.mark.asyncio
async def test_mint_test_token_is_user_scoped(tmp_path: Path) -> None:
    """Tokens are user-scoped (no org_id), so a minted token validates
    regardless of which org context the request happens to be in."""
    del tmp_path  # only needed by other tests in this module
    from mcpolis.entrypoints.controllers.gateway_controller import (
        current_org_id)
    from tests.unit.test_google_oauth import make_provider

    provider = make_provider()
    token_a = await provider.mint_test_token("alice@example.com")
    token_b = await provider.mint_test_token("bob@example.com")

    for active in ("org-a", "org-b", "default"):
        ctx = current_org_id.set(active)
        try:
            loaded_a = await provider.load_access_token(token_a)
            loaded_b = await provider.load_access_token(token_b)
            assert loaded_a is not None and loaded_a.client_id == "alice@example.com"
            assert loaded_b is not None and loaded_b.client_id == "bob@example.com"
        finally:
            current_org_id.reset(ctx)


def test_logout_revokes_session_cookie_server_side(
    tmp_path: Path,
) -> None:
    """After /logout, replaying the same cookie must fail authn.

    Regression for the pre-fix behaviour where /logout only deleted the
    client-side cookie; a stolen copy was still valid for up to 7 days.
    """
    client = make_test_client(tmp_path)
    cookie = client.cookies.get("mcpolis_session")
    assert cookie is not None

    # Sanity: the cookie authenticates before logout.
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"

    # Log out — Set-Cookie max-age=0 will clear the jar.
    logout = client.post("/api/auth/logout")
    assert logout.status_code == 204

    # Replay the "stolen" cookie by re-seeding it on the client jar
    # (per-request ``cookies=`` is deprecated in starlette TestClient).
    # Server-side revocation must reject it even though the HMAC is
    # still valid.
    client.cookies.set("mcpolis_session", cookie)
    replay = client.get("/api/auth/me")
    assert replay.status_code == 401


# --- Audit filters / admin-mcp tools / events / config / gateway ---


def test_admin_audit_filters_empty(tmp_path: Path) -> None:
    """``/audit/filters`` returns the dropdown values the admin UI
    needs to populate its user/upstream filter selects. Empty audit log
    must yield empty lists (not 404, not the raw config user list)."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/audit/filters")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"user_ids": [], "upstream_ids": []}


def test_admin_audit_filters_surfaces_log_values(tmp_path: Path) -> None:
    """Filter dropdowns must reflect the actual ids that appear in the
    log — not the config — so deleted users still show up while their
    history is being reviewed."""
    client = make_test_client(tmp_path)
    audit_path = tmp_path / "data" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"user_id": "alice", "upstream_id": "github"},
        {"user_id": "bob", "upstream_id": "mixpanel"},
        {"user_id": "alice", "upstream_id": "github"},
    ]
    with audit_path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    resp = client.get("/api/admin/audit/filters")
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_ids"] == ["alice", "bob"]
    assert data["upstream_ids"] == ["github", "mixpanel"]


def test_admin_admin_mcp_tools_returns_catalog(tmp_path: Path) -> None:
    """``/admin-mcp/tools`` returns the admin MCP server's tool catalog
    (the tools an admin can call from inside the chat sidebar). The
    list must be non-empty in the default app build, every entry must
    have ``name`` / ``description`` / ``input_schema`` / ``category``,
    and ``category`` is the bucket the frontend uses to group the
    tool picker."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/admin-mcp/tools")
    assert resp.status_code == 200
    tools = resp.json()
    assert isinstance(tools, list)
    assert len(tools) > 0
    for tool in tools:
        assert "name" in tool and isinstance(tool["name"], str)
        assert "description" in tool
        assert "input_schema" in tool
        assert "category" in tool


def test_config_gateway_returns_url_and_users(tmp_path: Path) -> None:
    """``/config/gateway`` is the source of truth for the dashboard's
    "Connect your MCP client" panel — URL the user pastes into Claude
    Desktop, plus the connected/all-users lists for the connections
    section. ``connected_users`` is intersected with this org's
    membership; ``all_users`` is the union (so admins see pre-approved
    members even before they sign in)."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/config/gateway")
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "http://localhost:8000/mcp"
    # Both seeded users are pre-approved members; neither has a live
    # gateway token yet, so connected is empty but all_users carries
    # both.
    assert data["connected_users"] == []
    assert sorted(data["all_users"]) == [
        "admin@example.com", "dev@example.com",
    ]


def test_config_gateway_requires_login(tmp_path: Path) -> None:
    """Anonymous callers must get 401 — the URL is public-ish but the
    member list isn't, and ``Depends(get_current_user)`` is the gate."""
    client = make_test_client(tmp_path, login=None)
    resp = client.get("/api/config/gateway")
    assert resp.status_code == 401


def test_events_stream_returns_sse(tmp_path: Path) -> None:
    """``/events`` opens an SSE stream — must respond 200 with the
    text/event-stream content type and the no-buffering headers the
    Vite/ngrok proxy relies on.

    The real event-bus subscribe loop is unbounded (the dashboard tab
    holds the connection open for the user's whole session), so we
    swap in a stream whose ``subscribe`` yields one event and then
    exits — that lets TestClient drain the response body without
    blocking. The route's contract under test is the wrapper, not the
    bus.
    """
    from collections.abc import AsyncIterator

    from mcpolis.domain.model.events import Event

    async def _one_event(
        org_id: str, user_email: str,
    ) -> AsyncIterator[Event | None]:
        del org_id, user_email
        yield Event(type="probe", payload={})

    client = make_test_client(tmp_path)
    with patch(
        "mcpolis.adapters.event_stream_inprocess"
        ".InProcessEventStream.subscribe",
        side_effect=_one_event,
    ):
        with client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith(
                "text/event-stream",
            )
            assert resp.headers["cache-control"] == "no-cache"
            assert resp.headers["x-accel-buffering"] == "no"
            body = b"".join(resp.iter_bytes())
    assert b"event: probe" in body


def test_events_stream_requires_login(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, login=None)
    resp = client.get("/api/events")
    assert resp.status_code == 401


def test_admin_disconnect_gateway_user_404_when_no_tokens(
    tmp_path: Path,
) -> None:
    """Revoking a user with no live gateway tokens must 404 — the
    operator's mental model is "this email isn't currently connected,
    so there's nothing to revoke." (The dashboard shows the button
    only for emails with active tokens, so a 404 here means the
    listing is stale; the frontend re-fetches on the error.)"""
    client = make_test_client(tmp_path)
    resp = client.delete("/api/admin/gateway/users/nobody@example.com")
    assert resp.status_code == 404


def test_admin_disconnect_gateway_user_revokes_live_tokens(
    tmp_path: Path,
) -> None:
    """Happy path: an admin revokes a connected user → underlying
    ``revoke_user_tokens`` returns the count, route returns 200 with
    a "Revoked N tokens" detail.

    The route closure captures ``gateway_provider.revoke_user_tokens``
    by reference at startup, so we can't class-level-patch it. Instead
    we seed the provider's in-memory token dict so the real revoker
    finds rows to delete.
    """
    from mcpolis.domain.ports.oauth_state_repository import (
        StoredAccessToken,
        StoredRefreshToken,
    )

    client = make_test_client(tmp_path)
    provider = client.app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined,union-attr]
    expires_at = int(__import__("time").time()) + 3600
    provider._access_tokens["t-access-1"] = StoredAccessToken(
        token="t-access-1",
        client_id="c1",
        user_email="alice@example.com",
        scopes=[],
        expires_at=expires_at,
    )
    provider._access_tokens["t-access-2"] = StoredAccessToken(
        token="t-access-2",
        client_id="c1",
        user_email="alice@example.com",
        scopes=[],
        expires_at=expires_at,
    )
    provider._refresh_tokens["t-refresh-1"] = StoredRefreshToken(
        token="t-refresh-1",
        client_id="c1",
        user_email="alice@example.com",
        scopes=[],
        created_at=0.0,
    )

    resp = client.delete("/api/admin/gateway/users/alice%40example.com")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "ok",
        "detail": "Revoked 3 tokens for alice@example.com",
    }
    # And the underlying dict is now empty for alice.
    assert all(
        t.user_email != "alice@example.com"
        for t in provider._access_tokens.values()
    )
    assert all(
        t.user_email != "alice@example.com"
        for t in provider._refresh_tokens.values()
    )


# --- Admin upstream-OAuth conflict (single-slot semantics) ---


def test_admin_connect_409_when_other_admin_owns_slot(
    tmp_path: Path,
) -> None:
    """Admin OAuth uses a single-slot model: at most one admin can
    hold the token row at a time. If admin B clicks Connect on an
    upstream where admin A already has a stored row, the route must
    409 with a "Disconnect first" message — the frontend keys off
    this to surface the take-over prompt.

    Setup: pre-seed the connection store with a token for a second
    admin (alice), then call /connect as the default admin
    (admin@example.com). The owner-detection scan must find alice's
    row and reject the take-over.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    from mcpolis.adapters.repositories.connection_store import OAuthToken
    from mcpolis.adapters.repositories.file_connection_store import (
        FileConnectionStore,
    )

    # Add alice as an admin so she's in the admin emails list (the
    # owner scan only iterates admin emails).
    client = make_test_client(tmp_path)
    add_user = client.post(
        "/api/admin/users",
        json={"email": "alice@example.com", "role": "admin"},
    )
    assert add_user.status_code == 201, add_user.text

    # Seed a stored OAuth token for mixpanel under alice. We have to
    # poke the file-store directly — the dashboard never writes a
    # token without going through the OAuth callback.
    store = FileConnectionStore(tmp_path / "data")

    async def _seed() -> None:
        await store.put_user_token(
            "default", "alice@example.com", "mixpanel",
            OAuthToken(
                access_token="alice-stored-token",
                refresh_token=None,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=[],
                updated_at=datetime.now(UTC),
            ),
        )
    asyncio.run(_seed())

    # admin@example.com tries to take over mixpanel without first
    # disconnecting alice → 409.
    resp = client.post("/api/admin/upstreams/mixpanel/connect")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "alice@example.com" in detail
    assert "Disconnect first" in detail


# --- Per-user OAuth connect / disconnect (auth_router) ---


def test_user_connect_404_on_unknown_upstream(tmp_path: Path) -> None:
    """``/api/auth/connect/{id}`` is the per-user OAuth entry point;
    an unknown upstream id must 404 before any token-store I/O."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/auth/connect/never-existed")
    assert resp.status_code == 404


def test_user_connect_400_on_service_account_upstream(tmp_path: Path) -> None:
    """``connect`` is OAuth-only — service_account upstreams have no
    user step to bind. Pinned because the route's auth-mode check is
    the boundary that keeps service_account out of the OAuth code
    path on the user surface (mirror of the admin-side
    ``connect_rejects_service_account``)."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/auth/connect/github")
    assert resp.status_code == 400
    assert "OAuth" in resp.json()["detail"]


def test_user_connect_returns_authorization_url(tmp_path: Path) -> None:
    """Happy path: per-user OAuth on an upstream with no stored token
    must return ``connected=False`` plus an ``authorization_url`` so
    the dashboard can redirect the user to the provider. The inner
    ``connect_and_refresh_tools`` is patched to avoid a real network
    handshake; we're pinning the route's response shape."""
    from mcpolis.domain.services.upstream_connection_service import (
        OAuthConnectResult,
    )

    client = make_test_client(tmp_path)
    fake = OAuthConnectResult(
        authorization_url="https://example.com/oauth/authorize?state=xyz",
    )
    with patch(
        "mcpolis.entrypoints.routes.dashboard.auth_connect"
        ".connect_and_refresh_tools",
        return_value=fake,
    ):
        resp = client.get("/api/auth/connect/mixpanel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is False
    assert body["authorization_url"] == (
        "https://example.com/oauth/authorize?state=xyz"
    )
    assert body["error"] is None


def test_user_disconnect_404_on_unknown_upstream(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post("/api/auth/disconnect/never-existed")
    assert resp.status_code == 404


def test_user_disconnect_clears_stored_token(tmp_path: Path) -> None:
    """``/api/auth/disconnect/{id}`` deletes the caller's stored
    OAuth row + tears down their per-user session. After the call,
    ``get_user_token`` returns ``None`` for the caller — the row that
    powered the previous /my-tools 'connected' state is gone."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from mcpolis.adapters.repositories.connection_store import OAuthToken
    from mcpolis.adapters.repositories.file_connection_store import (
        FileConnectionStore,
    )

    client = make_test_client(tmp_path)
    store = FileConnectionStore(tmp_path / "data")

    async def _seed_and_check_before() -> OAuthToken | None:
        await store.put_user_token(
            "default", "admin@example.com", "mixpanel",
            OAuthToken(
                access_token="caller-token",
                refresh_token=None,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=[],
                updated_at=datetime.now(UTC),
            ),
        )
        return await store.get_user_token(
            "default", "admin@example.com", "mixpanel",
        )

    seeded = asyncio.run(_seed_and_check_before())
    assert seeded is not None and seeded.access_token == "caller-token"

    resp = client.post("/api/auth/disconnect/mixpanel")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "disconnected"}

    async def _check_after() -> OAuthToken | None:
        return await store.get_user_token(
            "default", "admin@example.com", "mixpanel",
        )
    after = asyncio.run(_check_after())
    assert after is None, (
        f"caller's mixpanel token row should be gone after "
        f"/disconnect; got {after!r}"
    )


# --- Users-admin per-route coverage ---


def test_admin_add_user_409_on_duplicate(tmp_path: Path) -> None:
    """Re-adding an existing user must 409, not silently overwrite —
    the operator's mental model is "this email is already on the
    team," and a silent overwrite would clobber their role to the
    default. Important because admin@example.com is seeded by the
    test config; re-adding them should not flip them to developer."""
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/users",
        json={"email": "admin@example.com", "role": "developer"},
    )
    assert resp.status_code == 409
    # Sanity: their role didn't change.
    listing = client.get("/api/admin/users").json()
    admin_row = next(u for u in listing if u["email"] == "admin@example.com")
    assert admin_row["role"] == "admin"


def test_admin_add_user_400_on_unknown_role(tmp_path: Path) -> None:
    """A typo'd role must 400 with a helpful message — pre-add check
    catches the mistake before the user lands in the config with a
    dangling role reference."""
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/users",
        json={"email": "new@example.com", "role": "nonexistent"},
    )
    assert resp.status_code == 400
    assert "nonexistent" in resp.json()["detail"]


def test_admin_remove_user_404_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.delete("/api/admin/users/never@example.com")
    assert resp.status_code == 404


def test_admin_set_user_role_changes_role(tmp_path: Path) -> None:
    """``PUT /users/{email}/role`` changes the user's role. Pinned
    separately from add_user because the route also touches the
    membership row (when org_repo is wired) and notifies sessions —
    failure modes that ``add_and_remove_user`` doesn't exercise."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/users/dev@example.com/role",
        json={"role": "admin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "dev@example.com"
    assert body["role"] == "admin"
    assert body["is_admin"] is True


def test_admin_set_user_role_400_on_unknown_role(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/users/dev@example.com/role",
        json={"role": "nonexistent"},
    )
    assert resp.status_code == 400


# --- Upstream-admin per-route coverage ---


def test_admin_refresh_upstream_status_returns_summaries(
    tmp_path: Path,
) -> None:
    """``POST /upstreams/refresh-status`` must return the same shape as
    GET /upstreams (the dashboard re-uses the listing renderer for the
    response). Patches ``reconnect_all_oauth_upstreams`` to avoid a
    real network attempt; the route's interesting behavior is the
    summary build that follows."""
    client = make_test_client(tmp_path)
    with patch(
        "mcpolis.entrypoints.routes.dashboard.upstream_admin"
        ".reconnect_all_oauth_upstreams",
        return_value={},
    ):
        resp = client.post("/api/admin/upstreams/refresh-status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert {u["id"] for u in data} == {"github", "mixpanel"}


def test_admin_get_upstream_tools_returns_empty_for_unrefreshed(
    tmp_path: Path,
) -> None:
    """``/upstreams/{id}/tools`` returns the per-upstream tool catalog.
    Newly created test client has never connected the upstream so the
    catalog is empty — must still 200 (not 404, the upstream exists)
    with an empty list."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/github/tools")
    assert resp.status_code == 200
    assert resp.json() == []


def test_admin_get_upstream_tools_404_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/never-existed/tools")
    assert resp.status_code == 404


def test_admin_get_upstream_logs_returns_none_when_no_buffer(
    tmp_path: Path,
) -> None:
    """``/upstreams/{id}/logs`` returns ``{"logs": <str | None>}``.
    Service_account http upstream has no log buffer (logs are stdio-
    only), so the value is ``None`` — and the route still 200s."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/github/logs")
    assert resp.status_code == 200
    assert resp.json() == {"logs": None}


def test_admin_get_upstream_logs_404_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/never-existed/logs")
    assert resp.status_code == 404


def test_admin_stream_upstream_logs_404_on_unknown_upstream(
    tmp_path: Path,
) -> None:
    """``/upstreams/{id}/logs/stream`` 404 when the upstream itself
    doesn't exist (vs the no-log-buffer 404 below)."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/never-existed/logs/stream")
    assert resp.status_code == 404


def test_admin_stream_upstream_logs_404_on_no_buffer(tmp_path: Path) -> None:
    """``/upstreams/{id}/logs/stream`` 404 when the upstream exists
    but has no log buffer (an HTTP upstream has none)."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/upstreams/github/logs/stream")
    assert resp.status_code == 404


def test_admin_update_upstream_404_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/never-existed",
        json={"display_name": "Renamed"},
    )
    assert resp.status_code == 404


def test_admin_update_upstream_cosmetic_only(tmp_path: Path) -> None:
    """A display-name-only change writes the saved config and leaves
    the running session alone. Same shape as every other PUT now (no
    save flavour disconnects), but kept as the simplest happy-path
    pin — a regression that broke the persisted config write would
    fail this test first."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/upstreams/github",
        json={"display_name": "GitHub Renamed"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "GitHub Renamed"
    # Listing reflects the new name (proves the persisted config
    # changed, not just the response).
    listing = client.get("/api/admin/upstreams").json()
    github = next(u for u in listing if u["id"] == "github")
    assert github["display_name"] == "GitHub Renamed"


def test_admin_update_upstream_server_config_does_not_disconnect(
    tmp_path: Path,
) -> None:
    """Editing the JSON config of a running MCP must not tear down
    the live session. Operators apply the change explicitly via
    Stop+Start; the dashboard's ``DirtyConfigBanner`` (driven by
    ``UpstreamDetail.is_dirty``) tells them to. Pinned because the
    previous behaviour DID auto-disconnect, which dropped tokens
    and broke the user's mental model — see plan
    ``smooth-moseying-willow.md``."""
    client = make_test_client(tmp_path)
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.disconnect_upstream"
    ) as disconnect_mock:
        resp = client.put(
            "/api/admin/upstreams/github",
            json={"server_config": {"url": "http://localhost:9999/mcp"}},
        )
    assert resp.status_code == 200, resp.text
    assert disconnect_mock.call_count == 0, (
        "PUT with server_config change must not call disconnect_upstream;"
        f" got {disconnect_mock.call_args_list!r}"
    )


def test_admin_update_upstream_auth_mode_does_not_disconnect(
    tmp_path: Path,
) -> None:
    """Switching auth_mode is the heaviest non-resource change a PUT
    can carry; even it must leave the running session intact. The
    operator restarts when ready; the saved auth_mode applies on the
    next Start."""
    client = make_test_client(tmp_path)
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.disconnect_upstream"
    ) as disconnect_mock:
        resp = client.put(
            "/api/admin/upstreams/github",
            json={"auth_mode": "admin_oauth"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["auth_mode"] == "admin_oauth"
    assert disconnect_mock.call_count == 0


def test_admin_update_upstream_does_not_clear_oauth_tokens(
    tmp_path: Path,
) -> None:
    """Saved tokens belong to the runtime session; PUT is a config
    write, not a teardown. A regression that re-introduced the
    ``delete_all_upstream_tokens`` call from the old
    ``needs_disconnect`` branch would fail this — pre-seed a token,
    PUT a config change, observe the token still present."""
    client = make_test_client(tmp_path)
    with patch(
        "mcpolis.adapters.repositories.file_connection_store.FileConnectionStore.delete_all_upstream_tokens"
    ) as delete_tokens_mock, patch(
        "mcpolis.adapters.repositories.file_connection_store.FileConnectionStore.clear_connection_error"
    ) as clear_error_mock:
        resp = client.put(
            "/api/admin/upstreams/github",
            json={
                "auth_mode": "per_user_oauth",
                "server_config": {"url": "http://localhost:9999/mcp"},
            },
        )
    assert resp.status_code == 200, resp.text
    # Both runtime-state mutators stay untouched on save. Stop /
    # Start is the only path that should clear them.
    assert delete_tokens_mock.call_count == 0
    assert clear_error_mock.call_count == 0


def test_admin_update_upstream_400_on_stdio_disabled(
    tmp_path: Path,
) -> None:
    """When stdio is disabled (the cloud-mode-style toggle), an update
    that flips an upstream to a stdio command must 400 — not silently
    accept the field and leave the upstream half-configured."""
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
        allow_stdio_mcp=False,
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=True)
    login_as(client, "admin@example.com")

    resp = client.put(
        "/api/admin/upstreams/github",
        json={"server_config": {"command": "echo", "args": ["hello"]}},
    )
    assert resp.status_code == 400
    assert "Stdio" in resp.json()["detail"]


def test_admin_import_preview_returns_entries_and_existing_ids(
    tmp_path: Path,
) -> None:
    """``POST /upstreams/import/preview`` returns a flat ``entries`` list
    plus ``existing_ids``. A standard id that already exists is not
    dropped — its proposed id is auto-bumped (github → github-2) so the
    row stays selectable, and the existing ids are surfaced for the
    dialog's inline uniqueness check."""
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams/import/preview",
        json={
            "data": {
                "mcpServers": {
                    "github": {"url": "http://localhost:9999/mcp"},
                    "new-thing": {"url": "http://localhost:9998/mcp"},
                },
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_original = {e["original_id"]: e for e in body["entries"]}
    assert by_original["github"]["proposed_id"] == "github-2"
    assert by_original["new-thing"]["proposed_id"] == "new-thing"
    assert by_original["github"]["scope"] == "standard"
    assert {"github", "mixpanel"} <= set(body["existing_ids"])
    assert body["parse_errors"] == []


def test_admin_import_preview_claude_json_groups_and_flags_dupes(
    tmp_path: Path,
) -> None:
    """A ``.claude.json`` blob previews as grouped rows: user scope +
    one group per project, project ids suffixed with the basename, and
    byte-identical cross-project servers flagged via ``duplicate_of``."""
    client = make_test_client(tmp_path)
    same = {"url": "http://gh/mcp"}
    resp = client.post(
        "/api/admin/upstreams/import/preview",
        json={
            "data": {
                "mcpServers": {"sentry": {"url": "http://sentry/mcp"}},
                "projects": {
                    "/home/me/web": {"mcpServers": {"github": dict(same)}},
                    "/home/me/api": {"mcpServers": {"github": dict(same)}},
                },
            },
        },
    )
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    summary = [
        (e["scope"], e["group_label"], e["proposed_id"]) for e in entries
    ]
    assert summary == [
        ("user", "User scope", "sentry"),
        ("project", "web", "web-github"),
        ("project", "api", "api-github"),
    ]
    assert entries[1]["duplicate_of"] is None
    assert entries[2]["duplicate_of"] == {
        "proposed_id": "web-github", "group_label": "web",
    }


def test_admin_import_preview_400_on_single_entry(tmp_path: Path) -> None:
    """A single MCP entry (no ``mcpServers`` wrapper) must 400 with a
    helpful message pointing at "Add MCP" — the route is for bulk
    imports, not single-server registration."""
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams/import/preview",
        json={"data": {"url": "http://localhost:9999/mcp"}},
    )
    assert resp.status_code == 400
    assert "single MCP entry" in resp.json()["detail"]


def test_admin_import_preview_400_on_missing_wrapper(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams/import/preview",
        json={"data": {"random": "junk"}},
    )
    assert resp.status_code == 400
    assert "mcpServers" in resp.json()["detail"]


def test_admin_import_preview_partial_invalid_returns_array_fields(
    tmp_path: Path,
) -> None:
    """A file with an unbuildable server entry still returns 200 with
    ``entries`` / ``parse_errors`` / ``existing_ids`` ALL as arrays (never
    null or missing). This is the FE↔BE contract the import dialog relies
    on to not crash — regression for MCPOLIS-FRONTEND-B, where a missing
    list field threw ``Cannot read properties of undefined (reading
    'filter')`` the moment the preview landed."""
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/upstreams/import/preview",
        # url must be a string; a list makes build_upstream fail, so the
        # entry lands in parse_errors and ``entries`` is an empty array.
        json={"data": {"mcpServers": {"bad-entry": {"url": []}}}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["entries"], list)
    assert isinstance(body["parse_errors"], list)
    assert isinstance(body["existing_ids"], list)
    assert body["entries"] == []
    assert len(body["parse_errors"]) == 1


def test_admin_connect_upstream_admin_oauth_returns_authorization_url(
    tmp_path: Path,
) -> None:
    """Admin OAuth connect happy path: no token in store, no other
    admin owns the slot → ``connect_and_refresh_tools`` returns an
    ``authorization_url`` and the route surfaces it in
    ConnectResponse so the dashboard can redirect. Pinned because
    only the service-account-rejection branch was tested before."""
    from mcpolis.domain.services.upstream_connection_service import (
        OAuthConnectResult,
    )

    client = make_test_client(tmp_path)
    fake = OAuthConnectResult(
        authorization_url="https://example.com/oauth/authorize?state=abc",
    )
    with patch(
        "mcpolis.entrypoints.routes.dashboard.upstream_admin"
        ".connect_and_refresh_tools",
        return_value=fake,
    ):
        resp = client.post("/api/admin/upstreams/mixpanel/connect")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is False
    assert body["authorization_url"] == (
        "https://example.com/oauth/authorize?state=abc"
    )


# --- Roles per-route coverage ---


def test_admin_list_role_access_returns_per_role_settings(
    tmp_path: Path,
) -> None:
    """``GET /roles/access`` is the source for the Access page —
    every role appears with its mcp_access / tool_access /
    argument_constraints. Different from /roles (summary list) which
    only carries name + counts."""
    client = make_test_client(tmp_path)
    resp = client.get("/api/admin/roles/access")
    assert resp.status_code == 200
    data = resp.json()
    by_name = {r["name"]: r for r in data}
    assert {"admin", "developer"} <= set(by_name.keys())
    admin_row = by_name["admin"]
    assert admin_row["is_admin"] is True
    assert "mcp_access" in admin_row
    assert "tool_access" in admin_row
    assert "argument_constraints" in admin_row


def test_admin_set_role_mcp_access_replaces_full_block(
    tmp_path: Path,
) -> None:
    """``PUT /roles/{name}/mcp-access`` replaces the entire mcp_access
    block — the whole-object semantics matter: a partial update would
    drop unmentioned upstreams. Verified by writing a single-key block
    and checking the previous keys are gone."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/developer/mcp-access",
        json={"mcp_access": {"mcps": {"mixpanel": True}}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mcp_access"]["mcps"] == {"mixpanel": True}


def test_admin_set_role_mcp_access_404_on_unknown_role(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/nonexistent/mcp-access",
        json={"mcp_access": {"mcps": {}}},
    )
    assert resp.status_code == 404


def test_admin_set_role_mcp_access_entry_toggles_one_id(
    tmp_path: Path,
) -> None:
    """``PUT /roles/{name}/mcps/{mcp_id}`` is the per-cell toggle the
    Access page uses — leaves the other entries alone, only flips
    ``{mcp_id}: enabled``. Pinned because the Phase E backfill
    confirmed the mass mcp-access setter; this is the single-cell
    update the UI actually calls when an admin clicks one toggle."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/developer/mcps/mixpanel",
        json={"enabled": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mcp_access"]["mcps"]["mixpanel"] is True
    # github (originally True) untouched — partial update preserves
    # other entries.
    assert body["mcp_access"]["mcps"]["github"] is True


def test_admin_set_role_auto_enable_new_flips_flag(tmp_path: Path) -> None:
    """``PUT /roles/{name}/auto-enable-new`` controls whether newly
    added upstreams arrive enabled for this role. Default is False;
    flipping to True must round-trip into the role's mcp_access
    config."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/developer/auto-enable-new",
        json={"auto_enable_new": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mcp_access"]["auto_enable_new"] is True


def test_admin_set_role_tool_access_entry_toggles_tool(
    tmp_path: Path,
) -> None:
    """``PUT /roles/{name}/upstreams/{upstream_id}/tools/{tool_name}``
    flips per-tool allow for one role. Round-trips into
    ``tool_access[upstream_id].tools[tool_name] = enabled``."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/developer/upstreams/github/tools/list_repos",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_access"]["github"]["tools"]["list_repos"] is False


def test_admin_remove_role_tool_access_entry_clears_override(
    tmp_path: Path,
) -> None:
    """``DELETE`` of the same path drops the per-tool override so the
    role falls back to the upstream/category default."""
    client = make_test_client(tmp_path)
    # First set it
    client.put(
        "/api/admin/roles/developer/upstreams/github/tools/list_repos",
        json={"enabled": False},
    )
    resp = client.delete(
        "/api/admin/roles/developer/upstreams/github/tools/list_repos",
    )
    assert resp.status_code == 200
    body = resp.json()
    tools_for_github = (
        body["tool_access"].get("github", {}).get("tools", {})
    )
    assert "list_repos" not in tools_for_github


def test_admin_set_role_tool_fallback_enabled_round_trip(
    tmp_path: Path,
) -> None:
    """``PUT /roles/{name}/upstreams/{id}/tool-fallback-enabled`` sets
    the per-upstream "any tool not explicitly listed is allowed" flag
    — the role's catch-all for that upstream."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/developer/upstreams/github/tool-fallback-enabled",
        json={"fallback_enabled": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_access"]["github"]["fallback_enabled"] is False


def test_admin_set_role_tool_category_default_round_trip(
    tmp_path: Path,
) -> None:
    """``PUT /roles/{name}/upstreams/{id}/category-defaults/{ann}``
    sets the per-annotation default for one role+upstream
    (read-only-tools-allowed-by-default style toggles)."""
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/developer/upstreams/github/category-defaults/readOnlyHint",
        json={"enabled": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    cat = body["tool_access"]["github"]["category_defaults"]
    assert cat["readOnlyHint"] is True


def test_admin_remove_role_tool_category_default_clears_entry(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    client.put(
        "/api/admin/roles/developer/upstreams/github/category-defaults/readOnlyHint",
        json={"enabled": True},
    )
    resp = client.delete(
        "/api/admin/roles/developer/upstreams/github/category-defaults/readOnlyHint",
    )
    assert resp.status_code == 200
    body = resp.json()
    cat = (
        body["tool_access"].get("github", {}).get("category_defaults", {})
    )
    assert "readOnlyHint" not in cat


def test_admin_create_role_201_with_default_settings(
    tmp_path: Path,
) -> None:
    """``POST /roles`` creates a role with default settings (no
    mcp_access carry-over unless ``copy_from`` is supplied). 201
    status, role appears in subsequent /roles call."""
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/roles", json={"name": "viewer"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "viewer"
    listing = client.get("/api/admin/roles").json()
    assert "viewer" in {r["name"] for r in listing}


def test_admin_create_role_400_on_duplicate(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/roles", json={"name": "developer"})
    assert resp.status_code == 400


def test_admin_create_role_with_copy_from(tmp_path: Path) -> None:
    """``copy_from`` clones the source role's mcp/tool access — the
    "Duplicate" button on the Access page uses this."""
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/admin/roles",
        json={"name": "developer-clone", "copy_from": "developer"},
    )
    assert resp.status_code == 201
    body = resp.json()
    # Source role had github=True in its mcp_access; the clone must
    # inherit that.
    assert body["mcp_access"]["mcps"].get("github") is True


def test_admin_delete_role_removes_it(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    # Create a fresh role to delete (don't risk removing seed roles
    # that other tests share via the file_audit log).
    client.post("/api/admin/roles", json={"name": "tmp-role"})
    resp = client.delete("/api/admin/roles/tmp-role")
    assert resp.status_code == 200
    listing = client.get("/api/admin/roles").json()
    assert "tmp-role" not in {r["name"] for r in listing}


def test_admin_delete_role_400_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.delete("/api/admin/roles/never-existed")
    assert resp.status_code == 400


def test_admin_rename_role_updates_name(tmp_path: Path) -> None:
    """``PUT /roles/{name}/rename`` round-trips into the listing
    under the new name."""
    client = make_test_client(tmp_path)
    client.post("/api/admin/roles", json={"name": "to-rename"})
    resp = client.put(
        "/api/admin/roles/to-rename/rename",
        json={"new_name": "renamed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "renamed"
    listing = client.get("/api/admin/roles").json()
    names = {r["name"] for r in listing}
    assert "renamed" in names
    assert "to-rename" not in names


def test_admin_rename_role_400_on_unknown(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.put(
        "/api/admin/roles/never-existed/rename",
        json={"new_name": "whatever"},
    )
    assert resp.status_code == 400

