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
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.service_token_service import ServiceTokenService
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
    service_token_service: ServiceTokenService | None = None,
) -> tuple[Any, AuditRepository]:
    """Spin up a real admin-MCP server backed by file repos.

    Mirrors ``test_stdio_flag.test_stdio_flag_blocks_admin_mcp_tool``'s
    setup pattern: real ``UpstreamConfigService`` so ``list_upstreams``
    returns the seeded MCPs, real ``FileOrganizationRepository`` so
    ``resolve_plan`` reads the configured subscription. ``allow_stdio_mcp``
    is left True so the stdio cap test isn't masked by the feature flag.

    Pass *service_token_service* to wire the ``delete_role`` /
    ``list_roles`` service-token guard (AUTH-8); left ``None`` for the
    plan-gate tests that don't exercise it.
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
        service_token_service=service_token_service,
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


# ---------- refresh_upstream_tools (R6 recovery wiring) ----------


def _config_with_refreshable_upstreams() -> tuple[dict[str, Any], dict[str, Any]]:
    config = {
        "upstreams": {
            "github": {
                "display_name": "GitHub", "auth_mode": "service_account",
            },
            "notion": {
                "display_name": "Notion", "auth_mode": "per_user_oauth",
            },
        },
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }
    mcp_servers = {
        "github": {"url": "http://localhost:9000/mcp"},
        "notion": {"url": "http://localhost:9001/mcp"},
    }
    return config, mcp_servers


@pytest.mark.asyncio
async def test_refresh_upstream_tools_unknown_mcp_returns_not_found(
    tmp_path: Path,
) -> None:
    """Review item 8: the new early-return for an unknown ``mcp_id``."""
    config, mcp_servers = _config_with_refreshable_upstreams()
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )
    text = await _call(server, "refresh_upstream_tools", {"mcp_id": "ghost"})
    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_refresh_upstream_tools_session_unavailable_maps_to_message(
    tmp_path: Path,
) -> None:
    """Review item 8: a ``SessionUnavailable`` from the recovery path (an
    OAuth upstream with no stored tokens / no session) maps to the
    'could not reattach session' message, not a raw exception."""
    config, mcp_servers = _config_with_refreshable_upstreams()
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )
    text = await _call(server, "refresh_upstream_tools", {"mcp_id": "notion"})
    assert "could not reattach session" in text.lower()


@pytest.mark.asyncio
async def test_refresh_upstream_tools_single_routes_through_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 8: the ``mcp_id`` branch routes through
    ``acquire_and_refresh_with_recovery`` under the service_account identity
    (``effective_user=""``)."""
    config, mcp_servers = _config_with_refreshable_upstreams()
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )

    calls: list[tuple[str, str]] = []

    async def fake_recovery(**kwargs: Any) -> list[Any]:
        calls.append((kwargs["upstream"].id, kwargs["effective_user"]))
        return [object()]

    monkeypatch.setattr(
        "mcpolis.entrypoints.controllers.admin_mcp_controller"
        ".acquire_and_refresh_with_recovery",
        fake_recovery,
    )
    text = await _call(server, "refresh_upstream_tools", {"mcp_id": "github"})
    assert "Refreshed 1 tools" in text
    assert calls == [("github", "")], (
        "service_account refresh must route through the recovery helper "
        "under the shared identity"
    )


@pytest.mark.asyncio
async def test_refresh_upstream_tools_all_routes_through_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 8: the no-arg branch routes through
    ``refresh_all_with_recovery``."""
    config, mcp_servers = _config_with_refreshable_upstreams()
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )

    called = {"n": 0}

    async def fake_refresh_all(**_kwargs: Any) -> None:
        called["n"] += 1

    monkeypatch.setattr(
        "mcpolis.entrypoints.controllers.admin_mcp_controller"
        ".refresh_all_with_recovery",
        fake_refresh_all,
    )
    text = await _call(server, "refresh_upstream_tools", {})
    assert "Refreshed all upstream MCPs" in text
    assert called["n"] == 1, "the no-arg branch must call refresh_all_with_recovery"


# ---------- delete_role service-token guard (AUTH-8) ----------
#
# Sibling of the gateway-side AUTH-7 rejection: the admin-MCP
# ``delete_role`` tool refuses to drop a role that any service token is
# pinned to, because the token would silently fail closed (correct, but
# confusing when done by accident). The guard lives in
# ``admin_mcp_controller.py:980-987`` and reads ``count_by_role`` off the
# injected ``ServiceTokenService``. Revoking the token clears the guard.


def _config_with_custom_role(role_name: str) -> dict[str, Any]:
    """Admin + a deletable custom role (Team plan supports custom roles)."""
    return {
        "upstreams": {},
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
            role_name: {"settings": {"mcp_access": {"mcps": {}}}},
        },
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }


def _make_service_token_service(tmp_path: Path) -> ServiceTokenService:
    return ServiceTokenService(
        repo=FileServiceTokenRepository(tmp_path / "data"),
    )


@pytest.mark.asyncio
async def test_admin_mcp_delete_role_blocked_while_service_token_assigned(
    tmp_path: Path,
) -> None:
    """``delete_role`` fails with a 'service token' error while a token is
    pinned to the role; after revoke, the same call succeeds."""
    role_name = "reader"
    svc = _make_service_token_service(tmp_path)
    await svc.mint(
        org_id=DEFAULT_ORG_ID, label="ci-bot", role_name=role_name,
        created_by=ADMIN_EMAIL,
    )
    server, _ = await _build_admin_server(
        tmp_path,
        config=_config_with_custom_role(role_name),
        plan=PlanName.team,
        service_token_service=svc,
    )

    blocked = await _call(server, "delete_role", {"role_name": role_name})
    assert "service token" in blocked.lower()
    assert role_name.lower() in blocked.lower()

    # Revoke the token — the guard clears and deletion goes through.
    assert await svc.revoke(DEFAULT_ORG_ID, "ci-bot") is True
    deleted = await _call(server, "delete_role", {"role_name": role_name})
    assert "deleted" in deleted.lower()
    assert "service token" not in deleted.lower()


@pytest.mark.asyncio
async def test_admin_mcp_delete_role_unblocked_without_token_service(
    tmp_path: Path,
) -> None:
    """Control: with no service-token service wired, the guard is a no-op
    and an unused custom role deletes cleanly — proving the AUTH-8 block
    comes from the token guard, not some unrelated delete failure."""
    role_name = "reader"
    server, _ = await _build_admin_server(
        tmp_path,
        config=_config_with_custom_role(role_name),
        plan=PlanName.team,
    )
    deleted = await _call(server, "delete_role", {"role_name": role_name})
    assert "deleted" in deleted.lower()
