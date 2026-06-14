"""Tests for the admin MCP endpoint."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.adapters.repositories.file_upstream_config_store import (
    FileUpstreamConfigStore,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_config_service import UpstreamConfigService
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.admin_mcp_controller import (
    create_admin_mcp_server,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_user_id,
)
from tests.unit.factories import make_runtime_manager


def make_admin_test_app(tmp_path: Path) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "github": {"url": "http://localhost:9000/mcp"}
        }
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "upstreams": {
            "github": {"display_name": "GitHub", "auth_mode": "service_account"},
        },
        "roles": {
            "admin": {"is_admin": True, "settings": {"mcp_access": {"auto_enable_new": True}}},
        },
        "users": {},
    }))
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config_path,
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
    return TestClient(app, raise_server_exceptions=False)


def test_admin_mcp_requires_bearer_auth(tmp_path: Path) -> None:
    """The admin MCP surface is bearer-token protected. After Phase D's
    removal of the no-auth gateway path, an unauthenticated POST must
    fall over before anything else can — even in standalone+dev_stub
    mode the gateway provider's ``BearerAuthBackend`` runs."""
    client = make_admin_test_app(tmp_path)
    resp = client.post("/admin-mcp/")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Plan-gate parity coverage for the admin MCP surface.
#
# The dashboard tests in ``test_plan_gates.py`` exercise the same gates
# end-to-end through HTTP; these tests pin the helper wiring on the MCP
# tool path. They build the admin MCP server directly and drive each
# gated tool through ``server.call_tool`` so the plain-string error
# return shape (``"Error: …"``) is asserted against, not the
# dashboard's structured 402 body.
# ---------------------------------------------------------------------------

ADMIN_EMAIL = "admin@example.com"


def _config_users_only_admin() -> dict[str, Any]:
    return {
        "upstreams": {},
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }


def _config_with_three_seats() -> dict[str, Any]:
    return {
        "upstreams": {},
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {
            ADMIN_EMAIL: {"role": "admin"},
            "user2@example.com": {"role": "user"},
            "user3@example.com": {"role": "user"},
        },
    }


def _config_with_full_http_pool() -> tuple[dict[str, Any], dict[str, Any]]:
    upstreams = {
        f"u{i}": {"display_name": f"U{i}", "auth_mode": "service_account"}
        for i in range(5)
    }
    mcp_servers = {
        f"u{i}": {"url": f"http://localhost:90{i:02d}/mcp"} for i in range(5)
    }
    config: dict[str, Any] = {
        "upstreams": upstreams,
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }
    return config, mcp_servers


def _config_with_one_stdio() -> tuple[dict[str, Any], dict[str, Any]]:
    upstreams = {
        "s0": {"display_name": "S0", "auth_mode": "service_account"},
    }
    mcp_servers = {"s0": {"command": "echo"}}
    config: dict[str, Any] = {
        "upstreams": upstreams,
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }
    return config, mcp_servers


async def _build_admin_server(
    tmp_path: Path,
    *,
    config: dict[str, Any],
    mcp_servers: dict[str, Any] | None = None,
    plan: PlanName = PlanName.free,
) -> tuple[Any, AuditRepository]:
    """Spin up a real admin-MCP server backed by file repos.

    Mirrors ``test_stdio_flag.test_stdio_flag_blocks_admin_mcp_tool``'s
    setup pattern: real ``UpstreamConfigService`` so ``list_upstreams``
    returns the seeded MCPs, real ``FileOrganizationRepository`` so
    ``resolve_plan`` reads the configured subscription. ``allow_stdio_mcp``
    is left True so the stdio cap test isn't masked by the feature flag.
    """
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": mcp_servers or {}}))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    config_store = FileConfigStore(config_path)
    app_config = config_store.ensure_defaults_sync(DEFAULT_ORG_ID)
    policy_engine = PolicyEngine(app_config)
    mcp_store = McpJsonStore(mcp_json)
    upstream_store = FileUpstreamConfigStore(mcp_store, config_store)
    client_manager = UpstreamClientManager([])
    tool_registry = ToolRegistry([], client_manager)
    connection_store = FileConnectionStore(tmp_path)
    config_service = UpstreamConfigService(
        upstream_store, client_manager, tool_registry, connection_store,
    )
    audit_repo = FileAuditRepository(tmp_path / "data" / "audit.jsonl")
    org_repo = FileOrganizationRepository(tmp_path / "data")
    # Standalone now defaults the lone org to the unlimited Team plan, so
    # persist the requested plan explicitly (Free included) — these tests
    # assert the Free gate mechanics on an explicitly-Free org.
    await org_repo.update_subscription(
        DEFAULT_ORG_ID, Subscription(plan=plan),
    )

    rm = make_runtime_manager(
        policy_engine,
        tool_registry=tool_registry,
        client_manager=client_manager,
        config_service=config_service,
    )
    server = create_admin_mcp_server(
        runtime_manager=rm,
        audit_repo=audit_repo,
        policy_store=config_store,
        connection_store=connection_store,
        org_repo=org_repo,
    )
    return server, audit_repo


async def _call(server: Any, name: str, args: dict[str, Any]) -> str:
    """Invoke an admin-MCP tool with org / actor context set, return text."""
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    user_token = current_user_id.set(ADMIN_EMAIL)
    try:
        result: Any = await server.call_tool(name, args)
    finally:
        current_org_id.reset(org_token)
        current_user_id.reset(user_token)
    content_list = result[0]
    return str(content_list[0].text)


# ---------- add_user ----------


@pytest.mark.asyncio
async def test_admin_mcp_add_user_seat_gate_blocks_at_cap(
    tmp_path: Path,
) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=_config_with_three_seats(),
    )
    text = await _call(server, "add_user", {"email": "fourth@example.com"})
    assert text == "Error: Free plans are limited to 3 teammates."


@pytest.mark.asyncio
async def test_admin_mcp_add_user_under_cap_succeeds(tmp_path: Path) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )
    text = await _call(server, "add_user", {"email": "second@example.com"})
    payload = json.loads(text)
    assert payload["email"] == "second@example.com"


@pytest.mark.asyncio
async def test_admin_mcp_add_user_team_admits_above_free_cap(
    tmp_path: Path,
) -> None:
    server, _ = await _build_admin_server(
        tmp_path,
        config=_config_with_three_seats(),
        plan=PlanName.team,
    )
    text = await _call(server, "add_user", {"email": "fourth@example.com"})
    payload = json.loads(text)
    assert payload["email"] == "fourth@example.com"


# ---------- add_upstream (count gates) ----------


@pytest.mark.asyncio
async def test_admin_mcp_add_upstream_http_cap_blocks(
    tmp_path: Path,
) -> None:
    cfg, mcps = _config_with_full_http_pool()
    server, _ = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps,
    )
    text = await _call(server, "add_upstream", {
        "mcp_id": "u5",
        "display_name": "U5",
        "transport": "streamable_http",
        "url": "http://localhost:9100/mcp",
    })
    assert text == "Error: Free plans are limited to 5 remote HTTP MCPs."


@pytest.mark.asyncio
async def test_admin_mcp_add_upstream_stdio_cap_blocks(
    tmp_path: Path,
) -> None:
    cfg, mcps = _config_with_one_stdio()
    server, _ = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps,
    )
    text = await _call(server, "add_upstream", {
        "mcp_id": "s1",
        "display_name": "S1",
        "transport": "stdio",
        "command": "echo",
    })
    assert text == "Error: Free plans are limited to 1 hosted stdio MCP."


@pytest.mark.asyncio
async def test_admin_mcp_add_upstream_under_cap_succeeds(
    tmp_path: Path,
) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )
    text = await _call(server, "add_upstream", {
        "mcp_id": "first",
        "display_name": "First",
        "transport": "streamable_http",
        "url": "http://localhost:9000/mcp",
    })
    assert "added (disconnected)" in text


# ---------- create_role ----------


@pytest.mark.asyncio
async def test_admin_mcp_create_role_blocks_on_free(tmp_path: Path) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )
    text = await _call(server, "create_role", {"name": "viewer"})
    assert text == "Error: Free plans don't support custom roles."


@pytest.mark.asyncio
async def test_admin_mcp_create_role_team_succeeds(tmp_path: Path) -> None:
    server, _ = await _build_admin_server(
        tmp_path,
        config=_config_users_only_admin(),
        plan=PlanName.team,
    )
    text = await _call(server, "create_role", {"name": "viewer"})
    payload = json.loads(text)
    assert payload["name"] == "viewer"


# ---------- set_role_argument_constraint ----------


@pytest.mark.asyncio
async def test_admin_mcp_set_role_argument_constraint_blocks_on_free(
    tmp_path: Path,
) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )
    text = await _call(server, "set_role_argument_constraint", {
        "role_name": "admin",
        "upstream_id": "github",
        "tool_name": "create_issue",
        "arg_name": "repo",
        "pattern": ".*",
        "mode": "allow",
    })
    assert text == "Error: Argument checks aren't available on Free plans."


@pytest.mark.asyncio
async def test_admin_mcp_set_role_argument_constraint_team_passes_gate(
    tmp_path: Path,
) -> None:
    """Team plan: gate doesn't fire; the underlying mutation may
    serialize-fail downstream (the controller's JSON dump doesn't
    handle ArgumentConstraint pydantic models — pre-existing,
    orthogonal to plan gates), but it must not be a plan-gate error."""
    from mcp.server.fastmcp.exceptions import ToolError

    server, _ = await _build_admin_server(
        tmp_path,
        config=_config_users_only_admin(),
        plan=PlanName.team,
    )
    try:
        text = await _call(server, "set_role_argument_constraint", {
            "role_name": "admin",
            "upstream_id": "github",
            "tool_name": "create_issue",
            "arg_name": "repo",
            "pattern": ".*",
            "mode": "allow",
        })
    except ToolError as exc:
        text = str(exc)
    assert "Argument checks aren't available" not in text
