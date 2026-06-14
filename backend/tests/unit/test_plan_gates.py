"""Backend gate behaviour for the Free/Team plan mechanics.

Each test uses the standalone-mode TestClient pattern from
``test_dashboard_api.py`` and flips the standalone org's
subscription via the file repo to exercise both Free and Team paths.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as

ADMIN_EMAIL = "admin@example.com"

# Three pre-existing seats so the seat gate fires on the 4th add.
THREE_SEAT_USERS_JSON = {
    ADMIN_EMAIL: {"role": "admin"},
    "user2@example.com": {"role": "user"},
    "user3@example.com": {"role": "user"},
}


def _config_with_users(users: dict[str, dict[str, str]]) -> str:
    return json.dumps(
        {
            "upstreams": {},
            "roles": {
                "admin": {"is_admin": True},
                "user": {"is_default": True},
            },
            "users": users,
        }
    )


def _config_with_users_and_upstreams(
    users: dict[str, dict[str, str]],
    upstreams: dict[str, dict[str, object]],
    mcp_servers: dict[str, dict[str, object]],
) -> tuple[str, str]:
    config: dict[str, object] = {
        "upstreams": upstreams,
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": users,
    }
    return json.dumps(config), json.dumps({"mcpServers": mcp_servers})


def make_settings(tmp_path: Path, *, mcp_json: str, config_json: str) -> Settings:
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(mcp_json)
    config_path = tmp_path / "config.json"
    config_path.write_text(config_json)
    # Standalone now defaults the lone org to the unlimited Team plan, so
    # seed an explicit Free subscription: these tests exercise the Free
    # gate mechanics, and the Team-path tests overwrite this via
    # ``_flip_plan(settings, PlanName.team)`` before building the client.
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "subscription.json").write_text(
        json.dumps({"plan": PlanName.free.value})
    )
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
        allow_stdio_mcp=True,
    )


def make_client(settings: Settings, *, login: str | None = ADMIN_EMAIL) -> TestClient:
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all",
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all",
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=True)
    if login is not None:
        login_as(client, login)
    return client


def _flip_plan(settings: Settings, plan: PlanName) -> None:
    """Mutate the standalone org's subscription on disk so the next
    request sees the new plan. The file repo loads on construct, so
    we touch the same file the running app does (a fresh repo is
    constructed on the next request via the runtime cache hit and
    re-reads on init only — but the in-process repo holds a
    reference; we mutate it through a fresh repo write that updates
    the file, then call update_subscription on a fresh repo to
    trigger a rewrite. The simplest path: write the file directly).
    """
    data_dir = settings.data_dir
    repo = FileOrganizationRepository(data_dir)
    # Async helper synced via an event loop run.
    import asyncio

    asyncio.run(
        repo.update_subscription(DEFAULT_ORG_ID, Subscription(plan=plan)),
    )


def test_seat_gate_blocks_fourth_user_on_free(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users(THREE_SEAT_USERS_JSON),
    )
    client = make_client(settings)
    resp = client.post(
        "/api/admin/users",
        json={"email": "fourth@example.com", "role": "user"},
    )
    assert resp.status_code == 402
    body = resp.json()
    assert body["error"] == "plan_limit_exceeded"
    assert body["gate"] == "max_seats"
    assert body["limit"] == 3
    assert body["current"] == 3
    assert "3 teammates" in body["message"]


def test_seat_gate_admits_fourth_user_on_team(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users(THREE_SEAT_USERS_JSON),
    )
    _flip_plan(settings, PlanName.team)
    client = make_client(settings)
    resp = client.post(
        "/api/admin/users",
        json={"email": "fourth@example.com", "role": "user"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "fourth@example.com"


def test_http_upstream_gate_blocks_sixth_on_free(tmp_path: Path) -> None:
    upstreams: dict[str, dict[str, object]] = {
        f"u{i}": {"display_name": f"U{i}", "auth_mode": "service_account"}
        for i in range(5)
    }
    mcp_servers: dict[str, dict[str, object]] = {
        f"u{i}": {"url": f"http://localhost:90{i:02d}/mcp"} for i in range(5)
    }
    config_json, mcp_json = _config_with_users_and_upstreams(
        {ADMIN_EMAIL: {"role": "admin"}}, upstreams, mcp_servers,
    )
    settings = make_settings(tmp_path, mcp_json=mcp_json, config_json=config_json)
    client = make_client(settings)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "u5",
            "display_name": "U5",
            "url": "http://localhost:9100/mcp",
            "auth_mode": "service_account",
        },
    )
    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["gate"] == "max_http_upstreams"
    assert body["limit"] == 5
    assert body["current"] == 5


def test_stdio_upstream_gate_blocks_second_on_free(tmp_path: Path) -> None:
    upstreams: dict[str, dict[str, object]] = {
        "u0": {"display_name": "U0", "auth_mode": "service_account"},
    }
    mcp_servers: dict[str, dict[str, object]] = {"u0": {"command": "echo"}}
    config_json, mcp_json = _config_with_users_and_upstreams(
        {ADMIN_EMAIL: {"role": "admin"}}, upstreams, mcp_servers,
    )
    settings = make_settings(tmp_path, mcp_json=mcp_json, config_json=config_json)
    client = make_client(settings)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "u1",
            "display_name": "U1",
            "command": "echo",
            "auth_mode": "service_account",
        },
    )
    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["gate"] == "max_stdio_upstreams"
    assert body["limit"] == 1
    assert body["current"] == 1


def test_role_create_gate_blocks_third_role_on_free(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users({ADMIN_EMAIL: {"role": "admin"}}),
    )
    client = make_client(settings)
    resp = client.post("/api/admin/roles", json={"name": "viewer"})
    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["gate"] == "max_custom_roles"
    assert body["limit"] == 0


def test_role_create_admitted_on_team(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users({ADMIN_EMAIL: {"role": "admin"}}),
    )
    _flip_plan(settings, PlanName.team)
    client = make_client(settings)
    resp = client.post("/api/admin/roles", json={"name": "viewer"})
    assert resp.status_code == 201, resp.text


def test_argument_constraint_gate_blocks_on_free(tmp_path: Path) -> None:
    upstreams: dict[str, dict[str, object]] = {
        "github": {"display_name": "GitHub", "auth_mode": "service_account"},
    }
    mcp_servers: dict[str, dict[str, object]] = {
        "github": {"url": "http://localhost:9000/mcp"},
    }
    config_json, mcp_json = _config_with_users_and_upstreams(
        {ADMIN_EMAIL: {"role": "admin"}}, upstreams, mcp_servers,
    )
    settings = make_settings(tmp_path, mcp_json=mcp_json, config_json=config_json)
    client = make_client(settings)
    resp = client.put(
        "/api/admin/roles/admin/upstreams/github/tools/create_issue/constraints/repo",
        json={"pattern": ".*", "mode": "allow"},
    )
    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["gate"] == "allow_argument_constraints"


def test_argument_constraint_admitted_on_team(tmp_path: Path) -> None:
    upstreams_team: dict[str, dict[str, object]] = {
        "github": {"display_name": "GitHub", "auth_mode": "service_account"},
    }
    mcp_servers_team: dict[str, dict[str, object]] = {
        "github": {"url": "http://localhost:9000/mcp"},
    }
    upstreams = upstreams_team
    mcp_servers = mcp_servers_team
    config_json, mcp_json = _config_with_users_and_upstreams(
        {ADMIN_EMAIL: {"role": "admin"}}, upstreams, mcp_servers,
    )
    settings = make_settings(tmp_path, mcp_json=mcp_json, config_json=config_json)
    _flip_plan(settings, PlanName.team)
    client = make_client(settings)
    resp = client.put(
        "/api/admin/roles/admin/upstreams/github/tools/create_issue/constraints/repo",
        json={"pattern": ".*", "mode": "allow"},
    )
    # 200 only when the pattern lands successfully — argument
    # constraint creation against a tool that doesn't yet exist in
    # the registry is fine; the gate is the focus here.
    assert resp.status_code == 200, resp.text


def test_audit_retention_filter_caps_free_plan(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users({ADMIN_EMAIL: {"role": "admin"}}),
    )
    audit_path = tmp_path / "data" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    recent_iso = (now - timedelta(days=10)).isoformat()
    old_iso = (now - timedelta(days=60)).isoformat()
    rows = []
    for _ in range(5):
        rows.append({
            "user_id": "alice", "upstream_id": "github",
            "tool": "t", "policy_decision": "allowed",
            "timestamp": recent_iso,
        })
    for _ in range(5):
        rows.append({
            "user_id": "alice", "upstream_id": "github",
            "tool": "t", "policy_decision": "allowed",
            "timestamp": old_iso,
        })
    with audit_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    client = make_client(settings)
    resp = client.get("/api/admin/audit?limit=100")
    assert resp.status_code == 200
    assert resp.json()["count"] == 5  # only recent rows survive the 30-day cap

    _flip_plan(settings, PlanName.team)
    # New TestClient picks up the fresh subscription via the same
    # data_dir that the file repo reads on init.
    client_team = make_client(settings)
    resp_team = client_team.get("/api/admin/audit?limit=100")
    assert resp_team.status_code == 200
    assert resp_team.json()["count"] == 10  # 365-day cap covers both


def test_subscription_persistence_through_file_repo(tmp_path: Path) -> None:
    import asyncio

    async def _round_trip() -> None:
        repo = FileOrganizationRepository(tmp_path)
        org_before = await repo.get_organization(DEFAULT_ORG_ID)
        assert org_before is not None
        # Standalone defaults the lone org to the unlimited Team plan.
        assert org_before.subscription.plan == PlanName.team
        await repo.update_subscription(
            DEFAULT_ORG_ID, Subscription(plan=PlanName.free),
        )
        repo2 = FileOrganizationRepository(tmp_path)
        org_after = await repo2.get_organization(DEFAULT_ORG_ID)
        assert org_after is not None
        assert org_after.subscription.plan == PlanName.free

    asyncio.run(_round_trip())


def test_standalone_default_org_is_unlimited_team(tmp_path: Path) -> None:
    """M1: a self-hosted standalone install has no plan tier, so the lone
    org defaults to the unlimited Team plan — no Free-tier caps or
    upsells leak into the self-host. The Free gate would otherwise block
    a 6th HTTP upstream; on the default org it must not."""
    upstreams: dict[str, dict[str, object]] = {
        f"u{i}": {"display_name": f"U{i}", "auth_mode": "service_account"}
        for i in range(5)
    }
    mcp_servers: dict[str, dict[str, object]] = {
        f"u{i}": {"url": f"http://localhost:90{i:02d}/mcp"} for i in range(5)
    }
    config_json, mcp_json = _config_with_users_and_upstreams(
        {ADMIN_EMAIL: {"role": "admin"}}, upstreams, mcp_servers,
    )
    # Note: NOT seeding a Free subscription — exercise the real default.
    settings = make_settings(tmp_path, mcp_json=mcp_json, config_json=config_json)
    (settings.data_dir / "subscription.json").unlink()
    client = make_client(settings)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "u5",
            "display_name": "U5",
            "url": "http://localhost:9100/mcp",
            "auth_mode": "service_account",
        },
    )
    assert resp.status_code == 201, resp.text
    me = client.get("/api/auth/me").json()
    assert me["current_org"]["plan"] == "team"


def test_plan_limit_exception_handler_shape(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users(THREE_SEAT_USERS_JSON),
    )
    client = make_client(settings)
    resp = client.post(
        "/api/admin/users",
        json={"email": "fifth@example.com", "role": "user"},
    )
    assert resp.status_code == 402
    body = cast(dict[str, object], resp.json())
    # Documented JSON shape — every field present, every type as spec.
    assert set(body.keys()) == {"error", "gate", "current", "limit", "message"}
    assert body["error"] == "plan_limit_exceeded"


def test_superadmin_subscription_patch_flips_plan(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users({ADMIN_EMAIL: {"role": "admin"}}),
    )
    settings = settings.model_copy(
        update={"superadmin_emails": ADMIN_EMAIL},
    )
    client = make_client(settings)
    resp = client.patch(
        f"/api/superadmin/orgs/{DEFAULT_ORG_ID}/subscription",
        json={"plan": "team"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"] == "team"


def test_superadmin_subscription_patch_rejects_non_superadmin(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users({ADMIN_EMAIL: {"role": "admin"}}),
    )
    # ``ADMIN_EMAIL`` is dashboard-admin but NOT superadmin: 403.
    client = make_client(settings)
    resp = client.patch(
        f"/api/superadmin/orgs/{DEFAULT_ORG_ID}/subscription",
        json={"plan": "team"},
    )
    assert resp.status_code == 403


def test_me_endpoint_surfaces_plan(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users({ADMIN_EMAIL: {"role": "admin"}}),
    )
    client = make_client(settings)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_org"]["plan"] == "free"
    _flip_plan(settings, PlanName.team)
    client_team = make_client(settings)
    resp_team = client_team.get("/api/auth/me")
    assert resp_team.json()["current_org"]["plan"] == "team"


def test_sandbox_capabilities_marks_off_combos_disabled(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_json="{}",
        config_json=_config_with_users({ADMIN_EMAIL: {"role": "admin"}}),
    )
    client = make_client(settings)
    resp = client.get("/api/admin/sandbox/capabilities")
    assert resp.status_code == 200
    # Standalone test settings carry no E2B key, so the provider falls
    # back to local-subprocess, which does not enforce the picked combo
    # — the flag must reach the wire so the UI can disable the picker.
    assert resp.json()["provider"] == "local-subprocess"
    assert resp.json()["enforces_resources"] is False
    combos = resp.json()["allowed_combinations"]
    # The plan policy permits only (1, 1024) on Free. Provider may
    # expose more — those are surfaced with enabled=False.
    enabled_pairs = [
        (int(c["cpu_vcpus"]), c["memory_mb"])
        for c in combos if c["enabled"]
    ]
    assert (1, 1024) in enabled_pairs
    # If the provider advertises any non-(1, 1024) combo, it must
    # be disabled. (When the provider exposes only the single combo
    # the loop is empty.)
    for c in combos:
        pair = (int(c["cpu_vcpus"]), c["memory_mb"])
        if pair != (1, 1024):
            assert c["enabled"] is False, c
