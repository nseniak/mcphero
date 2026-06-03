"""Regression: super-admin upstream counts must read the *live* upstream
set, not the boot snapshot.

The bug: ``OrgRuntime.upstreams`` is a snapshot frozen at runtime
construction. The super-admin surfaces iterated it, while the per-org
admin routes read the persisted store. After an upstream was deleted it
lingered in the frozen snapshot, so the super-admin org list over-counted
(and the now-removed detail page showed a row that 404'd on click). The
fix points the super-admin surfaces at ``OrgRuntime.live_upstreams()``
(the registry, kept in sync on every add/remove), so the count tracks
the persisted store.

Mirrors the standalone-mode TestClient harness in ``test_plan_gates``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as

SUPERADMIN_EMAIL = "admin@example.com"
DEFAULT_SLUG = "default"


def make_settings_with_upstreams(
    tmp_path: Path,
    *,
    upstreams: dict[str, dict[str, object]],
    mcp_servers: dict[str, dict[str, object]],
) -> Settings:
    config = {
        "upstreams": upstreams,
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {SUPERADMIN_EMAIL: {"role": "admin"}},
    }
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": mcp_servers}))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
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
        superadmin_emails=SUPERADMIN_EMAIL,
    )


def make_superadmin_client(settings: Settings) -> TestClient:
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager."
        "UpstreamClientManager.start_all",
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all",
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=True)
    login_as(client, SUPERADMIN_EMAIL)
    return client


def _superadmin_upstream_count(client: TestClient, slug: str) -> int:
    resp = client.get("/api/superadmin/orgs")
    assert resp.status_code == 200, resp.text
    rows = [o for o in resp.json()["orgs"] if o["slug"] == slug]
    assert rows, f"org {slug} missing from super-admin list"
    return rows[0]["upstream_count"]


def test_deleted_upstream_drops_superadmin_count(tmp_path: Path) -> None:
    # Two HTTP service-account upstreams present at boot, so both land in
    # the runtime's frozen boot snapshot.
    settings = make_settings_with_upstreams(
        tmp_path,
        upstreams={
            "keep": {"display_name": "Keep", "auth_mode": "service_account"},
            "goner": {"display_name": "Goner", "auth_mode": "service_account"},
        },
        mcp_servers={
            "keep": {"url": "http://localhost:9001/mcp"},
            "goner": {"url": "http://localhost:9002/mcp"},
        },
    )
    client = make_superadmin_client(settings)

    # Both counted before deletion.
    assert _superadmin_upstream_count(client, DEFAULT_SLUG) == 2

    # Delete one through the per-org admin API (persisted store +
    # registry both updated; the boot snapshot is NOT).
    resp = client.delete("/api/admin/upstreams/goner")
    assert resp.status_code in (200, 204), resp.text

    # The super-admin count must reflect the deletion. With the bug
    # (reading ``runtime.upstreams``), it would stay at 2.
    assert _superadmin_upstream_count(client, DEFAULT_SLUG) == 1

    # And the canonical per-org detail agrees — the deleted upstream is
    # gone, so a drill-in 404s rather than showing an orphan row.
    detail = client.get("/api/admin/upstreams/goner")
    assert detail.status_code == 404


def test_added_upstream_raises_superadmin_count(tmp_path: Path) -> None:
    # Inverse of the staleness: an upstream added after boot is absent
    # from the frozen snapshot but present in the live registry.
    settings = make_settings_with_upstreams(
        tmp_path,
        upstreams={
            "keep": {"display_name": "Keep", "auth_mode": "service_account"},
        },
        mcp_servers={"keep": {"url": "http://localhost:9001/mcp"}},
    )
    client = make_superadmin_client(settings)

    assert _superadmin_upstream_count(client, DEFAULT_SLUG) == 1

    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "fresh",
            "display_name": "Fresh",
            "url": "http://localhost:9003/mcp",
            "auth_mode": "service_account",
        },
    )
    assert resp.status_code == 201, resp.text

    assert _superadmin_upstream_count(client, DEFAULT_SLUG) == 2
