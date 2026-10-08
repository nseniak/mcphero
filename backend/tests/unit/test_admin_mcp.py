"""Tests for the admin MCP endpoint."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.connection_store import OAuthToken
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
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.observability.analytics_client import (
    AnalyticsClient,
    get_analytics,
    set_analytics,
)
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.adapters.upstream_clients.upstream_state import (
    UpstreamConnectionState,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.services.admin_actions import AdminActionDeps
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_admin_service import (
    NewUpstreamRequest,
    TemplateVarInput,
    UpstreamAdminService,
)
from mcpolis.domain.services.upstream_config_service import UpstreamConfigService
from mcpolis.domain.services.upstream_connection_service import (
    OAuthConnectResult,
    OAuthFailureReason,
)
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.admin_mcp_controller import (
    create_admin_mcp_server,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_caller_id,
    current_org_id,
)
from tests.unit.factories import (
    GatedConfigStore,
    GatedTokenRepository,
    RecordingEventBus,
    RenameFailsOnceTokenRepository,
    make_audit_entry,
    make_runtime_manager,
    run_while_gated,
)


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


@dataclass
class AdminParts:
    """An Admin MCP server and what it runs on. ``action_deps`` builds
    a second door onto the same org runtime (the dashboard's, say)."""

    server: Any
    audit_repo: AuditRepository
    action_deps: AdminActionDeps


async def make_admin_parts(
    tmp_path: Path,
    *,
    config: dict[str, Any],
    mcp_servers: dict[str, Any] | None = None,
    plan: PlanName = PlanName.free,
    service_token_service: ServiceTokenService | None = None,
    auth_coordinator: PendingAuthCoordinator | None = None,
    client_manager: UpstreamClientManager | None = None,
    org_repo: FileOrganizationRepository | None = None,
    revoke_gateway_user: Callable[[str], int] | None = None,
    terminate_gateway_sessions: Callable[[str, str], Awaitable[int]] | None = None,
    event_bus: EventStream | None = None,
    config_store: FileConfigStore | None = None,
    connection_store: FileConnectionStore | None = None,
    audit_repo: AuditRepository | None = None,
    upstream_store: UpstreamConfigStore | None = None,
) -> AdminParts:
    """Spin up a real admin-MCP server backed by file repos.

    Mirrors ``test_stdio_flag.test_stdio_flag_blocks_admin_mcp_tool``'s
    setup pattern: real ``UpstreamConfigService`` so ``list_upstreams``
    returns the seeded MCPs, real ``FileOrganizationRepository`` so
    ``resolve_plan`` reads the configured subscription. ``allow_stdio_mcp``
    is left True so the stdio cap test isn't masked by the feature flag.

    Pass *service_token_service* to wire the ``delete_role`` /
    ``list_roles`` service-token guard (AUTH-8); left ``None`` for the
    plan-gate tests that don't exercise it.

    Pass *upstream_store* to keep upstreams somewhere other than the
    file store over ``tmp_path`` (e.g. the Mongo store).
    """
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": mcp_servers or {}}))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    # A given *config_store* must be built on ``tmp_path / "config.json"``.
    config_store = config_store or FileConfigStore(config_path)
    app_config = config_store.ensure_defaults_sync(DEFAULT_ORG_ID)
    policy_engine = PolicyEngine(app_config)
    if upstream_store is None:
        upstream_store = FileUpstreamConfigStore(
            McpJsonStore(mcp_json), config_store,
        )
    # Where the dashboard's test client keeps Variables too.
    template_var_repo = FileTemplateVarRepository(tmp_path / "data")
    if client_manager is None:
        client_manager = UpstreamClientManager(
            [], template_var_repo=template_var_repo,
        )
    tool_registry = ToolRegistry([], client_manager)
    # A given *connection_store* must be built on ``tmp_path``.
    connection_store = connection_store or FileConnectionStore(tmp_path)
    config_service = UpstreamConfigService(
        upstream_store, client_manager, tool_registry, connection_store,
        config_repo=config_store, policy_engine=policy_engine,
        template_var_repo=template_var_repo,
    )
    audit_repo = audit_repo or FileAuditRepository(
        tmp_path / "data" / "audit.jsonl",
    )
    if org_repo is None:
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
        auth_coordinator=auth_coordinator,
        revoke_gateway_user=revoke_gateway_user,
        terminate_gateway_sessions=terminate_gateway_sessions,
        event_bus=event_bus,
        template_var_repo=template_var_repo,
    )
    action_deps = AdminActionDeps(
        runtime_manager=rm,
        policy_store=config_store,
        audit_repo=audit_repo,
        connection_store=connection_store,
        auth_coordinator=auth_coordinator,
        server_url="http://localhost:8000",
        event_bus=event_bus,
        org_repo=org_repo,
        allow_stdio_mcp=True,
        revoke_gateway_user=revoke_gateway_user,
        terminate_gateway_sessions=terminate_gateway_sessions,
        service_token_service=service_token_service,
        template_var_repo=template_var_repo,
    )
    return AdminParts(server, audit_repo, action_deps)


async def _build_admin_server(
    tmp_path: Path,
    *,
    config: dict[str, Any],
    mcp_servers: dict[str, Any] | None = None,
    plan: PlanName = PlanName.free,
    service_token_service: ServiceTokenService | None = None,
    auth_coordinator: PendingAuthCoordinator | None = None,
    client_manager: UpstreamClientManager | None = None,
) -> tuple[Any, AuditRepository]:
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=plan,
        service_token_service=service_token_service,
        auth_coordinator=auth_coordinator, client_manager=client_manager,
    )
    return parts.server, parts.audit_repo


def make_bearer_user(email: str) -> AuthenticatedUser:
    """The identity a bearer token gives an Admin MCP request: the only
    one it has, since the dashboard cookie never reaches /admin-mcp."""
    return AuthenticatedUser(
        AccessToken(token="test-token", client_id=email, scopes=[]),
    )


async def _call(server: Any, name: str, args: dict[str, Any]) -> str:
    """Invoke an admin-MCP tool as ADMIN_EMAIL, authenticated the way
    production does it (bearer token), and return the text."""
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    auth_token = auth_context_var.set(make_bearer_user(ADMIN_EMAIL))
    try:
        result: Any = await server.call_tool(name, args)
    finally:
        current_org_id.reset(org_token)
        auth_context_var.reset(auth_token)
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
    assert "added" in text and not text.startswith("Error:")


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


# A single MCP's refresh is the dashboard's Refresh tools (a shared
# action): see ``test_admin_door_parity``.


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


# ---------- rename_role carries service tokens along ----------


@pytest.mark.asyncio
async def test_admin_mcp_rename_role_renames_it_on_its_tokens(
    tmp_path: Path,
) -> None:
    """Renaming a role through the Admin MCP moves its service tokens to
    the new name, and the policy notice goes out under the NEW name (the
    old name no longer matches anyone, so a notice under it reaches no
    session)."""
    svc = _make_service_token_service(tmp_path)
    await svc.mint(
        org_id=DEFAULT_ORG_ID, label="ci-bot", role_name="reader",
        created_by=ADMIN_EMAIL,
    )
    bus = RecordingEventBus()
    server = (await make_admin_parts(
        tmp_path,
        config=_config_with_custom_role("reader"),
        plan=PlanName.team,
        service_token_service=svc,
        event_bus=bus,
    )).server

    text = await _call(
        server, "rename_role", {"role_name": "reader", "new_name": "auditor"},
    )
    assert not text.startswith("Error"), text

    tokens = await svc.list_for_org(DEFAULT_ORG_ID)
    assert [(t.label, t.role_name) for t in tokens] == [("ci-bot", "auditor")]
    notices = [
        e.payload for e in bus.events if e.type == "policy_changed"
    ]
    assert notices == [{"role": "auditor"}]


# ---------- last-admin lockout guard ----------
# The admin MCP tools reach remove_user / set_user_role with no screen
# in the way, so the dashboard's frontend-only "you can't edit your own
# membership" rule never applied here at all. An assistant holding an
# admin token could empty the org of admins in one call.


def make_config_two_admins() -> dict[str, Any]:
    return {
        "upstreams": {},
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {
            ADMIN_EMAIL: {"role": "admin"},
            "deputy@example.com": {"role": "admin"},
        },
    }


@pytest.mark.asyncio
async def test_admin_mcp_remove_user_refuses_the_only_admin(
    tmp_path: Path,
) -> None:
    await seed_memberships(tmp_path, {ADMIN_EMAIL: "admin"})
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )
    text = await _call(server, "remove_user", {"email": ADMIN_EMAIL})
    assert text.startswith("Error:")
    assert "only admin" in text


@pytest.mark.asyncio
async def test_admin_mcp_set_user_role_refuses_demoting_the_only_admin(
    tmp_path: Path,
) -> None:
    await seed_memberships(tmp_path, {ADMIN_EMAIL: "admin"})
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )
    text = await _call(
        server, "set_user_role", {"email": ADMIN_EMAIL, "role": "user"},
    )
    assert text.startswith("Error:")
    assert "only admin" in text


@pytest.mark.asyncio
async def test_admin_mcp_remove_user_allows_one_of_two_admins(
    tmp_path: Path,
) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=make_config_two_admins(),
    )
    text = await _call(server, "remove_user", {"email": "deputy@example.com"})
    assert not text.startswith("Error:")


@pytest.mark.asyncio
async def test_admin_mcp_remove_user_closes_their_gateway_sessions(
    tmp_path: Path,
) -> None:
    """Removing a member closes their open gateway sessions in this org
    (awaited, so they are closed before the tool answers)."""
    await seed_memberships(
        tmp_path, {ADMIN_EMAIL: "admin", "deputy@example.com": "admin"},
    )
    terminate = AsyncMock(return_value=1)
    parts = await make_admin_parts(
        tmp_path,
        config=make_config_two_admins(),
        terminate_gateway_sessions=terminate,
    )

    text = await _call(parts.server, "remove_user", {"email": "deputy@example.com"})

    assert not text.startswith("Error:")
    terminate.assert_awaited_once_with(DEFAULT_ORG_ID, "deputy@example.com")


@pytest.mark.asyncio
async def test_admin_mcp_set_user_role_allows_demoting_one_of_two_admins(
    tmp_path: Path,
) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=make_config_two_admins(),
    )
    text = await _call(
        server,
        "set_user_role",
        {"email": "deputy@example.com", "role": "user"},
    )
    assert not text.startswith("Error:")


# ---------- Admin MCP vs dashboard parity ----------
#
# Each test below pins one place where the Admin MCP tool used to do
# less than the dashboard route for the same action.

DEPUTY_EMAIL = "deputy@example.com"


def make_oauth_upstream_config() -> tuple[dict[str, Any], dict[str, Any]]:
    config = make_config_two_admins()
    config["upstreams"] = {
        "notion": {"display_name": "Notion", "auth_mode": "per_user_oauth"},
    }
    mcp_servers = {"notion": {"url": "http://localhost:9001/mcp"}}
    return config, mcp_servers


def make_oauth_token() -> OAuthToken:
    return OAuthToken(
        access_token="access-123", refresh_token="refresh-456",
        expires_at=None, scopes=[],
    )


@pytest.mark.asyncio
async def test_admin_mcp_remove_user_deletes_the_membership_row(
    tmp_path: Path,
) -> None:
    """The dashboard's remove deletes the membership row; the Admin MCP
    tool must too, or the removed teammate keeps the org in their
    org list."""
    seed_repo = FileOrganizationRepository(tmp_path / "data")
    await seed_repo.add_membership(DEFAULT_ORG_ID, ADMIN_EMAIL, "admin")
    await seed_repo.add_membership(DEFAULT_ORG_ID, DEPUTY_EMAIL, "admin")
    server, _ = await _build_admin_server(
        tmp_path, config=make_config_two_admins(),
    )

    text = await _call(server, "remove_user", {"email": DEPUTY_EMAIL})

    assert not text.startswith("Error:"), text
    after = FileOrganizationRepository(tmp_path / "data")
    emails = {m.email for m in await after.list_memberships(DEFAULT_ORG_ID)}
    assert DEPUTY_EMAIL not in emails


@pytest.mark.asyncio
async def test_admin_mcp_add_upstream_persists_stopped(
    tmp_path: Path,
) -> None:
    """A server added through the Admin MCP starts stopped, like a
    dashboard add: the explicit marker keeps a restart from starting
    it before an admin clicks Start."""
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )

    text = await _call(server, "add_upstream", {
        "mcp_id": "first",
        "display_name": "First",
        "transport": "streamable_http",
        "url": "http://localhost:9000/mcp",
    })

    assert "added" in text, text
    store = FileConnectionStore(tmp_path)
    assert await store.is_enabled(DEFAULT_ORG_ID, "first") is False


@pytest.mark.asyncio
async def test_admin_mcp_connect_refuses_while_another_admin_holds_per_user_slot(
    tmp_path: Path,
) -> None:
    """The dashboard refuses a per-user sign-in while another admin
    holds the server's single admin slot; the Admin MCP must refuse
    it the same way, not only for admin sign-in."""
    config, mcp_servers = make_oauth_upstream_config()
    seed_store = FileConnectionStore(tmp_path)
    await seed_store.put_user_token(
        DEFAULT_ORG_ID, DEPUTY_EMAIL, "notion", make_oauth_token(),
    )
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers,
        auth_coordinator=PendingAuthCoordinator(b"k" * 32),
    )

    text = await _call(server, "connect_upstream", {"mcp_id": "notion"})

    assert DEPUTY_EMAIL in text
    assert "already signed in" in text


class RecordingClientManager(UpstreamClientManager):
    """Records which servers had their sandbox storage cleaned up."""

    def __init__(self) -> None:
        super().__init__([])
        self.cleaned: list[str] = []

    async def cleanup_sandbox_state_for_upstream(
        self, upstream_id: str,
    ) -> None:
        self.cleaned.append(upstream_id)


def make_admin_slot_config() -> tuple[dict[str, Any], dict[str, Any]]:
    config = make_config_two_admins()
    config["upstreams"] = {
        "linear": {"display_name": "Linear", "auth_mode": "admin_oauth"},
    }
    mcp_servers = {"linear": {"url": "http://localhost:9002/mcp"}}
    return config, mcp_servers


@pytest.mark.asyncio
async def test_admin_mcp_remove_upstream_cleans_up_sandbox_storage(
    tmp_path: Path,
) -> None:
    """Removing a server through the Admin MCP must clean up its
    sandbox storage, like the dashboard's remove."""
    cfg, mcps = _config_with_one_stdio()
    manager = RecordingClientManager()
    server, _ = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
    )

    text = await _call(server, "remove_upstream", {"mcp_id": "s0"})

    assert "removed" in text, text
    assert manager.cleaned == ["s0"]


@pytest.mark.asyncio
async def test_admin_mcp_disconnect_keeps_the_admin_sign_in(
    tmp_path: Path,
) -> None:
    """The dashboard's Stop keeps every saved sign-in, the admin's
    included, so Start needs nobody to sign in again; the Admin MCP
    disconnect must too. The server is stopped all the same."""
    config, mcp_servers = make_admin_slot_config()
    seed_store = FileConnectionStore(tmp_path)
    await seed_store.put_user_token(
        DEFAULT_ORG_ID, DEPUTY_EMAIL, "linear", make_oauth_token(),
    )
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers,
    )

    text = await _call(server, "disconnect_upstream", {"mcp_id": "linear"})

    assert "disconnected" in text, text
    store = FileConnectionStore(tmp_path)
    assert await store.get_user_token(
        DEFAULT_ORG_ID, DEPUTY_EMAIL, "linear",
    ) is not None
    assert await store.is_enabled(DEFAULT_ORG_ID, "linear") is False


@pytest.mark.asyncio
async def test_admin_mcp_set_user_role_updates_the_membership_row(
    tmp_path: Path,
) -> None:
    """The dashboard's role change also updates the teammate's
    membership row; the Admin MCP role change must too."""
    seed_repo = FileOrganizationRepository(tmp_path / "data")
    await seed_repo.add_membership(DEFAULT_ORG_ID, ADMIN_EMAIL, "admin")
    await seed_repo.add_membership(DEFAULT_ORG_ID, DEPUTY_EMAIL, "admin")
    server, _ = await _build_admin_server(
        tmp_path, config=make_config_two_admins(),
    )

    text = await _call(
        server, "set_user_role", {"email": DEPUTY_EMAIL, "role": "user"},
    )

    assert not text.startswith("Error:"), text
    after = FileOrganizationRepository(tmp_path / "data")
    roles = {
        m.email: m.role for m in await after.list_memberships(DEFAULT_ORG_ID)
    }
    assert roles[DEPUTY_EMAIL] == "user"


@pytest.mark.asyncio
async def test_admin_mcp_acts_as_the_bearer_token_user(tmp_path: Path) -> None:
    """An Admin MCP request carries only a bearer token, never the
    dashboard cookie. Its actions must still be recorded under the
    admin who called, not under "anonymous"."""
    cfg, mcps = _config_with_one_stdio()
    server, audit_repo = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps,
        client_manager=ConnectsClientManager(),
    )

    with patch.object(ToolRegistry, "refresh_upstream", AsyncMock(return_value=[])):
        await _call(server, "start_upstream", {"mcp_id": "s0"})
    await _call(server, "disconnect_upstream", {"mcp_id": "s0"})

    entries = await audit_repo.search(DEFAULT_ORG_ID, limit=5)
    assert sorted((e["action"], e["user_id"]) for e in entries) == [
        ("disconnect", ADMIN_EMAIL), ("reconnect", ADMIN_EMAIL),
    ]


class StopsStartAtOnceClientManager(UpstreamClientManager):
    """Cancels every Start as soon as it is scheduled, like a Stop
    landing before the Start's first step."""

    def __init__(self) -> None:
        super().__init__([])

    def register_background_connect_task(
        self, upstream_id: str, task: asyncio.Task[None],
    ) -> None:
        super().register_background_connect_task(upstream_id, task)
        task.cancel()


@pytest.mark.asyncio
async def test_admin_mcp_start_reports_a_stop_that_lands_first(
    tmp_path: Path,
) -> None:
    """A Start cancelled before it runs must answer at once, not after
    the tool's whole wait."""
    cfg, mcps = _config_with_one_stdio()
    server, _ = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps,
        client_manager=StopsStartAtOnceClientManager(),
    )

    text = await asyncio.wait_for(
        _call(server, "start_upstream", {"mcp_id": "s0"}), timeout=5,
    )

    assert text == (
        "The start of 's0' was interrupted by a stop or a newer start."
    )



# ---------- Review follow-ups (independent review, 2026-10-07) ----------


def mark_connected(manager: UpstreamClientManager, upstream_id: str) -> None:
    """What a Start's successful connect leaves behind: the upstream is
    LIVE, and the Start is no longer its tracked background task, so a
    Stop no longer cancels it."""
    state = manager.get_state(upstream_id)
    assert state is not None
    state.state = UpstreamConnectionState.LIVE
    state.background_task = None


class ConnectsClientManager(UpstreamClientManager):
    """A Start whose connect succeeds at once. Records every disconnect
    (a running session torn down)."""

    def __init__(self) -> None:
        super().__init__([])
        self.disconnects: list[str] = []

    async def connect_upstream(
        self, upstream: UpstreamDefinition, bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> Any:
        del bearer_token, auth
        mark_connected(self, upstream.id)
        return MagicMock()

    async def disconnect_upstream(
        self, upstream_id: str, *, reset_state: bool = True,
    ) -> None:
        self.disconnects.append(upstream_id)
        await super().disconnect_upstream(upstream_id, reset_state=reset_state)


class ConnectsThenPausesClientManager(UpstreamClientManager):
    """A Start whose connect goes live, then pauses before returning, so
    a Stop can land after the connect went live but before the Start
    answers."""

    def __init__(self) -> None:
        super().__init__([])
        self.connected = asyncio.Event()
        self.release = asyncio.Event()

    async def connect_upstream(
        self, upstream: UpstreamDefinition, bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> Any:
        del bearer_token, auth
        mark_connected(self, upstream.id)
        self.connected.set()
        await self.release.wait()
        return MagicMock()


class BlocksConnectClientManager(UpstreamClientManager):
    """A Start whose connect never finishes on its own."""

    def __init__(self) -> None:
        super().__init__([])
        self.entered = asyncio.Event()

    async def connect_upstream(
        self, upstream: UpstreamDefinition, bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> Any:
        del upstream, bearer_token, auth
        self.entered.set()
        await asyncio.Event().wait()


class FailingConnectClientManager(UpstreamClientManager):
    """A Start whose connect fails."""

    def __init__(self) -> None:
        super().__init__([])

    async def connect_upstream(
        self, upstream: UpstreamDefinition, bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> Any:
        del upstream, bearer_token, auth
        raise RuntimeError("connection refused")


class FailingDisconnectClientManager(UpstreamClientManager):
    """A Stop whose session teardown fails."""

    def __init__(self) -> None:
        super().__init__([])

    async def disconnect_upstream(
        self, upstream_id: str, *, reset_state: bool = True,
    ) -> None:
        del upstream_id, reset_state
        raise RuntimeError("sandbox API down")


class FlakyMembershipRepo(FileOrganizationRepository):
    """A membership store that fails once, then works again."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.failures_left = 1

    async def remove_membership(self, org_id: str, email: str) -> None:
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("membership store unavailable")
        await super().remove_membership(org_id, email)


class RecordingAnalytics(AnalyticsClient):
    """Keeps every tracked event instead of sending it."""

    def __init__(self) -> None:
        super().__init__(token="", super_properties={})
        self.tracked: list[tuple[str, str]] = []

    def track_async(
        self,
        distinct_id: str,
        event: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        del properties
        self.tracked.append((distinct_id, event))


async def sign_in_finishing_later(**kwargs: Any) -> OAuthConnectResult:
    """A sign-in whose tokens arrive later through the OAuth callback,
    while an earlier, abandoned sign-in fails."""
    kwargs["on_tokens_acquired"]()
    if kwargs["on_error"] is not None:
        kwargs["on_error"]("denied", OAuthFailureReason.user_denied)
    return OAuthConnectResult(
        authorization_url="https://idp.example/authorize?state=s",
    )


@pytest.mark.asyncio
async def test_admin_mcp_start_is_interrupted_by_a_stop_after_its_connect(
    tmp_path: Path,
) -> None:
    """Once a Start's connect is live, a Stop no longer cancels it. The
    Start must then neither claim it started nor audit a success."""
    cfg, mcps = _config_with_one_stdio()
    manager = ConnectsThenPausesClientManager()
    server, audit_repo = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
    )

    start = asyncio.create_task(_call(server, "start_upstream", {"mcp_id": "s0"}))
    await asyncio.wait_for(manager.connected.wait(), 5)
    await _call(server, "disconnect_upstream", {"mcp_id": "s0"})
    manager.release.set()
    text = await asyncio.wait_for(start, 10)

    assert text == (
        "The start of 's0' was interrupted by a stop or a newer start."
    )
    entries = await audit_repo.search(DEFAULT_ORG_ID, limit=5)
    assert [(e["action"], e["outcome"]) for e in entries] == [
        ("disconnect", "success"),
    ]


@pytest.mark.asyncio
async def test_admin_mcp_start_is_interrupted_by_a_stop_that_a_newer_start_lifted(
    tmp_path: Path,
) -> None:
    """Start A's connect is live; a Stop closes its session, and Start B
    starts the MCP again before A answers. The MCP is no longer stopped,
    but the session A connected is gone: A must still say it was
    interrupted, and only B audits a successful Start."""
    cfg, mcps = _config_with_one_stdio()
    manager = ConnectsThenPausesClientManager()
    server, audit_repo = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
    )

    start_a = asyncio.create_task(
        _call(server, "start_upstream", {"mcp_id": "s0"}),
    )
    await asyncio.wait_for(manager.connected.wait(), 5)
    await _call(server, "disconnect_upstream", {"mcp_id": "s0"})
    manager.connected.clear()
    start_b = asyncio.create_task(
        _call(server, "start_upstream", {"mcp_id": "s0"}),
    )
    await asyncio.wait_for(manager.connected.wait(), 5)
    manager.release.set()
    text_a = await asyncio.wait_for(start_a, 10)
    await asyncio.wait_for(start_b, 10)

    assert text_a == (
        "The start of 's0' was interrupted by a stop or a newer start."
    )
    entries = await audit_repo.search(DEFAULT_ORG_ID, limit=10)
    assert [e["outcome"] for e in entries if e["action"] == "reconnect"] == [
        "success",
    ]


@pytest.mark.asyncio
async def test_admin_mcp_connect_reports_a_failed_tool_discovery(
    tmp_path: Path,
) -> None:
    """A sign-in that connects but whose tool discovery fails must say
    so, not "Discovered 0 tools"."""
    config, mcp_servers = make_oauth_upstream_config()
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers,
        auth_coordinator=PendingAuthCoordinator(b"k" * 32),
    )
    with patch(
        "mcpolis.domain.services.upstream_connection_service"
        ".initiate_oauth_connection",
        AsyncMock(return_value=OAuthConnectResult(connected=True)),
    ), patch(
        "mcpolis.domain.services.upstream_connection_service"
        "._MIN_REFRESHING_DISPLAY_SECONDS",
        0.0,
    ), patch.object(
        ToolRegistry, "refresh_upstream",
        AsyncMock(side_effect=RuntimeError("list_tools timed out")),
    ):
        text = await asyncio.wait_for(
            _call(server, "connect_upstream", {"mcp_id": "notion"}), 10,
        )

    assert text == (
        "MCP 'notion' is connected but tool discovery failed: "
        "list_tools timed out"
    )


@pytest.mark.asyncio
async def test_admin_mcp_start_reports_a_failed_tool_discovery(
    tmp_path: Path,
) -> None:
    cfg, mcps = _config_with_one_stdio()
    server, _ = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps,
        client_manager=ConnectsClientManager(),
    )
    with patch.object(
        ToolRegistry, "refresh_upstream",
        AsyncMock(side_effect=RuntimeError("list_tools timed out")),
    ):
        text = await asyncio.wait_for(
            _call(server, "start_upstream", {"mcp_id": "s0"}), 10,
        )

    assert text == (
        "MCP 's0' started, but tool discovery failed: list_tools timed out"
    )


@pytest.mark.asyncio
async def test_admin_mcp_start_says_when_a_newer_start_replaced_it(
    tmp_path: Path,
) -> None:
    """The dashboard's Start restarts an upstream that is starting. The
    Admin MCP call waiting on the first Start answers at once."""
    cfg, mcps = _config_with_one_stdio()
    manager = BlocksConnectClientManager()
    parts = await make_admin_parts(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
    )
    dashboard = UpstreamAdminService(parts.action_deps)

    first = asyncio.create_task(
        _call(parts.server, "start_upstream", {"mcp_id": "s0"}),
    )
    await asyncio.wait_for(manager.entered.wait(), 5)
    await dashboard.start_upstream(DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL)
    first_text = await asyncio.wait_for(first, 5)
    await _call(parts.server, "disconnect_upstream", {"mcp_id": "s0"})

    assert first_text == (
        "The start of 's0' was interrupted by a stop or a newer start."
    )


@pytest.mark.asyncio
async def test_admin_mcp_start_leaves_a_starting_upstream_alone(
    tmp_path: Path,
) -> None:
    cfg, mcps = _config_with_one_stdio()
    manager = BlocksConnectClientManager()
    server, _ = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
    )

    first = asyncio.create_task(_call(server, "start_upstream", {"mcp_id": "s0"}))
    await asyncio.wait_for(manager.entered.wait(), 5)
    again = await asyncio.wait_for(
        _call(server, "start_upstream", {"mcp_id": "s0"}), 5,
    )
    await _call(server, "disconnect_upstream", {"mcp_id": "s0"})
    await asyncio.wait_for(first, 5)

    assert again == (
        "MCP 's0' is already starting. Run upstream_status in a moment "
        "to check."
    )


@pytest.mark.asyncio
async def test_admin_mcp_start_leaves_a_running_upstream_alone(
    tmp_path: Path,
) -> None:
    """start_upstream says it is idempotent and not destructive, so on a
    running upstream it must not tear the live session down."""
    cfg, mcps = _config_with_one_stdio()
    manager = ConnectsClientManager()
    server, _ = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
    )
    with patch.object(ToolRegistry, "refresh_upstream", AsyncMock(return_value=[])):
        first = await _call(server, "start_upstream", {"mcp_id": "s0"})
    manager.disconnects.clear()

    again = await _call(server, "start_upstream", {"mcp_id": "s0"})

    assert first == "MCP 's0' started. 0 tools available."
    assert again == "MCP 's0' is already running. 0 tools available."
    assert manager.disconnects == []
    annotations = {t.name: t for t in await server.list_tools()}[
        "start_upstream"
    ].annotations
    assert annotations is not None
    assert annotations.idempotentHint is True
    assert annotations.destructiveHint is False


@pytest.mark.asyncio
async def test_admin_mcp_start_refuses_while_another_admin_holds_the_slot(
    tmp_path: Path,
) -> None:
    """Start on an OAuth upstream signs the caller in, so it follows
    Connect's rule: never a second admin behind the single slot."""
    config, mcp_servers = make_admin_slot_config()
    seed = FileConnectionStore(tmp_path)
    await seed.put_user_token(
        DEFAULT_ORG_ID, DEPUTY_EMAIL, "linear", make_oauth_token(),
    )
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers,
        auth_coordinator=PendingAuthCoordinator(b"k" * 32),
    )
    sign_in = AsyncMock(return_value=OAuthConnectResult(
        authorization_url="https://idp.example/authorize?state=s",
    ))
    with patch(
        "mcpolis.domain.services.upstream_admin_service"
        ".connect_and_refresh_tools",
        sign_in,
    ):
        text = await _call(server, "start_upstream", {"mcp_id": "linear"})

    assert sign_in.await_count == 0
    assert text == f"Error: '{DEPUTY_EMAIL}' is already signed in to this MCP."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("connect_upstream", ["upstream_tokens_acquired", "upstream_oauth_error"]),
        ("start_upstream", ["upstream_tokens_acquired"]),
    ],
)
async def test_admin_mcp_sign_in_tells_a_waiting_dashboard_tab(
    tmp_path: Path, tool: str, expected: list[str],
) -> None:
    """Connect and Start both announce the tokens of a sign-in that
    finishes later. Only Connect announces a failed sign-in: an earlier,
    abandoned sign-in can fail late and would read as the newer one's
    failure."""
    config, mcp_servers = make_oauth_upstream_config()
    bus = RecordingEventBus()
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        auth_coordinator=PendingAuthCoordinator(b"k" * 32), event_bus=bus,
    )
    with patch(
        "mcpolis.domain.services.upstream_admin_service"
        ".connect_and_refresh_tools",
        sign_in_finishing_later,
    ):
        await _call(parts.server, tool, {"mcp_id": "notion"})

    sign_in_events = [
        e.type for e in bus.events if e.type.startswith("upstream_")
    ]
    assert sign_in_events == expected


@pytest.mark.asyncio
async def test_admin_mcp_remove_user_ends_access_even_if_the_membership_store_fails(
    tmp_path: Path,
) -> None:
    """Access goes first: a failing membership store must not leave the
    removed teammate's gateway tokens alive, and removing them again
    finishes the job."""
    revoked: list[str] = []
    repo = FlakyMembershipRepo(tmp_path / "data")
    await repo.add_membership(DEFAULT_ORG_ID, ADMIN_EMAIL, "admin")
    await repo.add_membership(DEFAULT_ORG_ID, DEPUTY_EMAIL, "admin")
    parts = await make_admin_parts(
        tmp_path, config=make_config_two_admins(), org_repo=repo,
        revoke_gateway_user=lambda email: revoked.append(email) or 1,
    )

    with pytest.raises(ToolError):
        await _call(parts.server, "remove_user", {"email": DEPUTY_EMAIL})
    revoked_after_failure = list(revoked)
    retry = await _call(parts.server, "remove_user", {"email": DEPUTY_EMAIL})

    assert revoked_after_failure == [DEPUTY_EMAIL]
    assert retry == f"User '{DEPUTY_EMAIL}' removed."
    emails = {m.email for m in await repo.list_memberships(DEFAULT_ORG_ID)}
    assert DEPUTY_EMAIL not in emails


@pytest.mark.asyncio
async def test_add_upstream_saves_nothing_when_variables_cannot_be_stored(
    tmp_path: Path,
) -> None:
    """Actions built without a Variables store: an add that brings
    Variables anyway must fail before saving anything."""
    parts = await make_admin_parts(tmp_path, config=make_config_two_admins())
    service = UpstreamAdminService(
        replace(parts.action_deps, template_var_repo=None),
    )

    with pytest.raises(RuntimeError):
        await service.add_upstream(
            DEFAULT_ORG_ID,
            NewUpstreamRequest(
                id="vars", display_name="Vars",
                url="http://localhost:9000/mcp",
                template_vars={"API_KEY": TemplateVarInput(value="x")},
            ),
            actor=ADMIN_EMAIL, source="test",
        )

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert await runtime.config_service.get_upstream(DEFAULT_ORG_ID, "vars") is None


@pytest.mark.asyncio
async def test_admin_mcp_add_upstream_refuses_an_id_the_dashboard_cannot_type(
    tmp_path: Path,
) -> None:
    """Upstream ids become tool-name prefixes; an AI assistant must not
    be able to create ``GitHub Tools__search``."""
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )

    text = await _call(server, "add_upstream", {
        "mcp_id": "GitHub Tools",
        "display_name": "GitHub",
        "transport": "streamable_http",
        "url": "http://localhost:9000/mcp",
    })

    assert text == (
        "Error: Invalid id (allowed: lowercase letters, digits, hyphens, "
        "underscores, dots)"
    )


@pytest.mark.asyncio
async def test_a_failed_add_leaves_no_upstream_behind(tmp_path: Path) -> None:
    """A step failing after the save (here the per-role access entries)
    removes the upstream again, so no restart starts a half-added one,
    and with it the Variable its ``auth_token`` was saved as."""
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
    )
    with patch.object(
        FileConfigStore, "create_mcp_access",
        AsyncMock(side_effect=OSError("config store unavailable")),
    ), pytest.raises(ToolError):
        await _call(server, "add_upstream", {
            "mcp_id": "half",
            "display_name": "Half",
            "transport": "streamable_http",
            "url": "http://localhost:9000/mcp",
            "auth_token": "tok-secret-123456",
        })

    saved = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]
    assert "half" not in saved
    variables = FileTemplateVarRepository(tmp_path / "data")
    assert await variables.list_summaries(DEFAULT_ORG_ID, "half") == []


@pytest.mark.asyncio
async def test_admin_mcp_start_that_crashes_answers_with_the_error(
    tmp_path: Path,
) -> None:
    """The Start's own bookkeeping fails after a successful connect: the
    Admin MCP answers with the error at once instead of waiting."""
    cfg, mcps = _config_with_one_stdio()
    server, _ = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps,
        client_manager=ConnectsClientManager(),
    )
    with patch.object(
        FileConnectionStore, "clear_connection_error",
        AsyncMock(side_effect=[None, OSError("disk full")]),
    ):
        text = await asyncio.wait_for(
            _call(server, "start_upstream", {"mcp_id": "s0"}), 5,
        )

    assert text == "Error starting 's0': disk full"


@pytest.mark.asyncio
async def test_a_client_giving_up_on_start_leaves_the_start_running(
    tmp_path: Path,
) -> None:
    """A client that gives up (its tool call is cancelled) must not
    cancel the Start itself."""
    cfg, mcps = _config_with_one_stdio()
    manager = ConnectsThenPausesClientManager()
    server, audit_repo = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
    )
    call = asyncio.create_task(_call(server, "start_upstream", {"mcp_id": "s0"}))
    await asyncio.wait_for(manager.connected.wait(), 5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    with patch.object(ToolRegistry, "refresh_upstream", AsyncMock(return_value=[])):
        manager.release.set()
        entries: list[dict[str, Any]] = []
        for _ in range(100):
            entries = await audit_repo.search(DEFAULT_ORG_ID, limit=5)
            if entries:
                break
            await asyncio.sleep(0.02)

    assert [(e["action"], e["outcome"]) for e in entries] == [
        ("reconnect", "success"),
    ]


@pytest.mark.asyncio
async def test_admin_mcp_connect_answers_while_tool_discovery_still_runs(
    tmp_path: Path,
) -> None:
    config, mcp_servers = make_oauth_upstream_config()
    server, _ = await _build_admin_server(
        tmp_path, config=config, mcp_servers=mcp_servers,
        auth_coordinator=PendingAuthCoordinator(b"k" * 32),
    )
    release = asyncio.Event()

    async def slow_refresh(*_args: Any, **_kwargs: Any) -> list[Any]:
        await release.wait()
        return []

    with patch(
        "mcpolis.domain.services.upstream_connection_service"
        ".initiate_oauth_connection",
        AsyncMock(return_value=OAuthConnectResult(connected=True)),
    ), patch(
        "mcpolis.domain.services.upstream_connection_service"
        "._MIN_REFRESHING_DISPLAY_SECONDS",
        0.0,
    ), patch(
        "mcpolis.entrypoints.controllers.admin_mcp_controller"
        "._TOOL_DISCOVERY_WAIT_SECONDS",
        0.05,
    ), patch.object(ToolRegistry, "refresh_upstream", slow_refresh):
        text = await asyncio.wait_for(
            _call(server, "connect_upstream", {"mcp_id": "notion"}), 5,
        )
        release.set()
        await asyncio.sleep(0.01)

    assert text == (
        "MCP 'notion' is connected; tool discovery is still running. "
        "Run list_upstream_tools in a moment."
    )


@pytest.mark.asyncio
async def test_admin_mcp_disconnect_failure_is_audited_and_reported(
    tmp_path: Path,
) -> None:
    """A Stop whose session teardown fails reports the error, audits it,
    and does not mark the upstream stopped: it may still be running."""
    cfg, mcps = _config_with_one_stdio()
    server, audit_repo = await _build_admin_server(
        tmp_path, config=cfg, mcp_servers=mcps,
        client_manager=FailingDisconnectClientManager(),
    )

    with pytest.raises(ToolError, match="sandbox API down"):
        await _call(server, "disconnect_upstream", {"mcp_id": "s0"})

    entries = await audit_repo.search(DEFAULT_ORG_ID, limit=5)
    assert [(e["action"], e["outcome"]) for e in entries] == [
        ("disconnect", "error"),
    ]
    store = FileConnectionStore(tmp_path)
    assert await store.is_enabled(DEFAULT_ORG_ID, "s0") is True


@pytest.mark.asyncio
async def test_admin_mcp_actions_are_tracked_under_the_bearer_user(
    tmp_path: Path,
) -> None:
    cfg, mcps = _config_with_one_stdio()
    server, _ = await _build_admin_server(tmp_path, config=cfg, mcp_servers=mcps)
    analytics = RecordingAnalytics()
    previous = get_analytics()
    set_analytics(analytics)
    try:
        await _call(server, "remove_upstream", {"mcp_id": "s0"})
    finally:
        set_analytics(previous)

    assert analytics.tracked == [(ADMIN_EMAIL, "upstream_removed")]


@pytest.mark.asyncio
async def test_admin_mcp_upstream_status_shows_stopped_and_failed(
    tmp_path: Path,
) -> None:
    """start_upstream points at upstream_status, so upstream_status must
    show what a Start leaves behind."""
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(),
        client_manager=FailingConnectClientManager(),
    )
    await _call(server, "add_upstream", {
        "mcp_id": "first",
        "display_name": "First",
        "transport": "streamable_http",
        "url": "http://localhost:9000/mcp",
    })
    stopped = json.loads(await _call(server, "upstream_status", {}))
    start = await _call(server, "start_upstream", {"mcp_id": "first"})
    failed = json.loads(await _call(server, "upstream_status", {}))

    assert stopped == {"first": "stopped"}
    assert start == "Error starting 'first': connection refused"
    assert failed == {"first": "failed: connection refused"}


@pytest.mark.asyncio
async def test_session_handlers_see_the_openers_auth_context() -> None:
    """The MCP SDK runs every request of a session inside the task made
    at ``initialize``: a request from someone else on the same session
    id is handled with the opener's ``auth_context_var``. So the Admin
    MCP's ``current_caller_id()`` names the opener, and only the session
    owner guard (a session serves only its opener) makes that safe."""
    server = FastMCP(name="probe", json_response=True, streamable_http_path="/")

    @server.tool(name="whoami")
    async def whoami() -> str:  # pyright: ignore[reportUnusedFunction]
        return current_caller_id()

    inner = server.streamable_http_app()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        headers = dict(scope["headers"])
        user = headers.get(b"x-user", b"").decode()
        token = auth_context_var.set(make_bearer_user(user))
        org = current_org_id.set(DEFAULT_ORG_ID)
        try:
            await inner(scope, receive, send)
        finally:
            auth_context_var.reset(token)
            current_org_id.reset(org)

    base_headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    async with server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
        ) as client:
            init = await client.post("/", headers={
                **base_headers, "x-user": "alice@a.com",
            }, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0"},
                },
            })
            assert init.status_code == 200, init.text
            sid = init.headers["mcp-session-id"]
            await client.post("/", headers={
                **base_headers, "x-user": "alice@a.com", "mcp-session-id": sid,
            }, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            call = await client.post("/", headers={
                **base_headers, "x-user": "bob@b.com", "mcp-session-id": sid,
            }, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            })

    assert call.status_code == 200, call.text
    assert call.json()["result"]["content"][0]["text"] == "alice@a.com"

# ---------- membership rows follow Admin MCP user / role edits ----------
# A membership keeps its own copy of the user's role. The dashboard keeps
# it in sync; these pin the same for the Admin MCP door.


async def seed_memberships(tmp_path: Path, rows: dict[str, str]) -> None:
    """Write membership rows to disk before the server's repo loads them."""
    repo = FileOrganizationRepository(tmp_path / "data")
    for email, role in rows.items():
        await repo.add_membership(DEFAULT_ORG_ID, email, role)


async def read_membership_roles(tmp_path: Path) -> dict[str, str]:
    repo = FileOrganizationRepository(tmp_path / "data")
    return {m.email: m.role for m in await repo.list_memberships(DEFAULT_ORG_ID)}


@pytest.mark.asyncio
async def test_admin_mcp_rename_role_renames_it_on_memberships(
    tmp_path: Path,
) -> None:
    config = _config_with_custom_role("reader")
    config["users"]["reader@example.com"] = {"role": "reader"}
    await seed_memberships(
        tmp_path, {ADMIN_EMAIL: "admin", "reader@example.com": "reader"},
    )
    server, _ = await _build_admin_server(
        tmp_path, config=config, plan=PlanName.team,
    )
    text = await _call(
        server, "rename_role", {"role_name": "reader", "new_name": "auditor"},
    )
    assert not text.startswith("Error"), text
    assert await read_membership_roles(tmp_path) == {
        ADMIN_EMAIL: "admin", "reader@example.com": "auditor",
    }


@pytest.mark.asyncio
async def test_admin_mcp_set_user_role_keeps_invited_user_pending(
    tmp_path: Path,
) -> None:
    """An invited user who never signed in has no membership row. A role
    change must not create one, or the Team page would show them as
    joined."""
    config = make_config_two_admins()
    config["users"]["invitee@example.com"] = {"role": "user"}
    await seed_memberships(
        tmp_path, {ADMIN_EMAIL: "admin", "deputy@example.com": "admin"},
    )
    server, _ = await _build_admin_server(tmp_path, config=config)
    text = await _call(
        server, "set_user_role",
        {"email": "invitee@example.com", "role": "admin"},
    )
    assert not text.startswith("Error"), text
    assert await read_membership_roles(tmp_path) == {
        ADMIN_EMAIL: "admin", "deputy@example.com": "admin",
    }


async def listed_role_names(server: Any) -> set[str]:
    return {r["name"] for r in json.loads(await _call(server, "list_roles", {}))}


@pytest.mark.asyncio
async def test_admin_mcp_failed_token_write_leaves_running_policy_on_the_stored_rename(
    tmp_path: Path,
) -> None:
    """The running policy is reloaded right after the stored rename,
    before the token write. When the token write fails, the tool reports
    an error, and renaming the role back repairs the state."""
    token_repo = RenameFailsOnceTokenRepository(tmp_path / "data")
    svc = ServiceTokenService(repo=token_repo)
    await svc.mint(
        org_id=DEFAULT_ORG_ID, label="ci-bot", role_name="reader",
        created_by=ADMIN_EMAIL,
    )
    server, _ = await _build_admin_server(
        tmp_path,
        config=_config_with_custom_role("reader"),
        plan=PlanName.team,
        service_token_service=svc,
    )

    with pytest.raises(ToolError, match="token store unavailable"):
        await _call(
            server, "rename_role",
            {"role_name": "reader", "new_name": "auditor"},
        )
    names = await listed_role_names(server)
    assert "auditor" in names
    assert "reader" not in names
    assert [(t.label, t.role_name) for t in await svc.list_for_org(
        DEFAULT_ORG_ID,
    )] == [("ci-bot", "reader")]

    back = await _call(
        server, "rename_role", {"role_name": "auditor", "new_name": "reader"},
    )
    assert not back.startswith("Error"), back
    again = await _call(
        server, "rename_role", {"role_name": "reader", "new_name": "auditor"},
    )
    assert not again.startswith("Error"), again
    assert [(t.label, t.role_name) for t in await svc.list_for_org(
        DEFAULT_ORG_ID,
    )] == [("ci-bot", "auditor")]


@pytest.mark.asyncio
async def test_admin_mcp_delete_waits_for_a_rename_in_progress(
    tmp_path: Path,
) -> None:
    """A delete that runs its token check before a rename has moved the
    tokens counts zero, and deletes the role the tokens are moving to."""
    token_repo = GatedTokenRepository(tmp_path / "data", gated="rename_role")
    svc = ServiceTokenService(repo=token_repo)
    await svc.mint(
        org_id=DEFAULT_ORG_ID, label="ci-bot", role_name="reader",
        created_by=ADMIN_EMAIL,
    )
    server, _ = await _build_admin_server(
        tmp_path,
        config=_config_with_custom_role("reader"),
        plan=PlanName.team,
        service_token_service=svc,
    )

    renamed, deleted, overlapped = await run_while_gated(
        token_repo.gate,
        lambda: _call(
            server, "rename_role",
            {"role_name": "reader", "new_name": "auditor"},
        ),
        lambda: _call(server, "delete_role", {"role_name": "auditor"}),
    )

    assert not overlapped
    assert not renamed.startswith("Error"), renamed
    assert "service token" in deleted.lower()
    assert "auditor" in await listed_role_names(server)
    assert [(t.label, t.role_name) for t in await svc.list_for_org(
        DEFAULT_ORG_ID,
    )] == [("ci-bot", "auditor")]


# ---------- a cancelled call never stops a role change half-way ----------
# Tested over the real MCP protocol, the client's ``notifications/cancelled``
# included, in ``test_admin_mcp_cancelled_calls``.


@pytest.mark.asyncio
async def test_admin_mcp_add_user_is_refused_when_its_role_is_renamed_mid_way(
    tmp_path: Path,
) -> None:
    """Adding a user checks the role, then saves. A rename in between
    must not leave the new user on a role that no longer exists."""
    config_store = GatedConfigStore(tmp_path / "config.json", gated="set_user")
    server = (await make_admin_parts(
        tmp_path,
        config=_config_with_custom_role("reader"),
        plan=PlanName.team,
        config_store=config_store,
    )).server

    added, renamed, overlapped = await run_while_gated(
        config_store.gate,
        lambda: _call(
            server, "add_user",
            {"email": "new@example.com", "role": "reader"},
        ),
        lambda: _call(
            server, "rename_role",
            {"role_name": "reader", "new_name": "auditor"},
        ),
    )

    # The rename really landed between the check and the save.
    assert overlapped
    assert not renamed.startswith("Error"), renamed
    assert added == "Error: Role 'reader' not found"
    users = (await config_store.load(DEFAULT_ORG_ID)).users
    assert "new@example.com" not in users


# ---------- audit rows ----------


@pytest.mark.asyncio
async def test_admin_mcp_remove_user_writes_an_audit_row(tmp_path: Path) -> None:
    server, audit_repo = await _build_admin_server(
        tmp_path, config=make_config_two_admins(),
    )

    await _call(server, "remove_user", {"email": "deputy@example.com"})

    rows = await audit_repo.search(
        DEFAULT_ORG_ID, action=["member_removed"], limit=10,
    )
    assert len(rows) == 1
    assert rows[0]["user_id"] == ADMIN_EMAIL
    assert rows[0]["target_user_id"] == "deputy@example.com"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["actor_role"] is None


@pytest.mark.asyncio
async def test_admin_mcp_audit_search_applies_the_plan_retention_cap(
    tmp_path: Path,
) -> None:
    """A Free org reads 30 days of audit rows on the dashboard; the
    Admin MCP search tool must stop at the same 30 days."""
    server, audit_repo = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(), plan=PlanName.free,
    )
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    recent = datetime.now(UTC).isoformat()
    await audit_repo.log(DEFAULT_ORG_ID, make_audit_entry(
        user_id="old-row", timestamp=old,
    ))
    await audit_repo.log(DEFAULT_ORG_ID, make_audit_entry(
        user_id="recent-row", timestamp=recent,
    ))

    text = await _call(server, "search_audit_log", {})

    rows = json.loads(text)
    assert [r["user_id"] for r in rows] == ["recent-row"]
def make_config_two_signed_in_admins_and_an_invited_one() -> dict[str, Any]:
    return {
        "upstreams": {},
        "roles": {"admin": {"is_admin": True}, "user": {"is_default": True}},
        "users": {
            ADMIN_EMAIL: {"role": "admin"},
            "deputy@example.com": {"role": "admin"},
            "inv@example.com": {"role": "admin"},
        },
    }


async def make_server_with_stale_team_view(tmp_path: Path) -> Any:
    """admin@ and deputy@ have signed in; inv@ is only invited. Then
    deputy@ is removed behind the server's back (as a parallel request
    would), so its in-memory view still counts deputy@."""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    (data / "memberships.json").write_text(json.dumps([
        {"org_id": DEFAULT_ORG_ID, "email": e, "role": "admin", "created_at": now}
        for e in (ADMIN_EMAIL, "deputy@example.com")
    ]))
    server, _ = await _build_admin_server(
        tmp_path, config=make_config_two_signed_in_admins_and_an_invited_one(),
    )
    path = tmp_path / "config.json"
    raw: dict[str, Any] = json.loads(path.read_text())
    del raw["users"]["deputy@example.com"]
    path.write_text(json.dumps(raw))
    return server


def users_on_disk(tmp_path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((tmp_path / "config.json").read_text())
    return raw["users"]


@pytest.mark.asyncio
async def test_admin_mcp_store_refuses_removal_a_stale_pre_check_let_through(
    tmp_path: Path,
) -> None:
    server = await make_server_with_stale_team_view(tmp_path)

    text = await _call(server, "remove_user", {"email": ADMIN_EMAIL})

    assert text.startswith("Error:") and "only admin" in text, text
    assert ADMIN_EMAIL in users_on_disk(tmp_path)


@pytest.mark.asyncio
async def test_admin_mcp_store_refuses_demotion_a_stale_pre_check_let_through(
    tmp_path: Path,
) -> None:
    server = await make_server_with_stale_team_view(tmp_path)

    text = await _call(
        server, "set_user_role", {"email": ADMIN_EMAIL, "role": "user"},
    )

    assert text.startswith("Error:") and "only admin" in text, text
    assert users_on_disk(tmp_path)[ADMIN_EMAIL]["role"] == "admin"


@pytest.mark.asyncio
async def test_admin_mcp_add_user_refuses_an_address_added_meanwhile(
    tmp_path: Path,
) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(), plan=PlanName.team,
    )
    path = tmp_path / "config.json"
    raw: dict[str, Any] = json.loads(path.read_text())
    raw["users"]["new@example.com"] = {"role": "admin"}
    path.write_text(json.dumps(raw))

    text = await _call(
        server, "add_user", {"email": "new@example.com", "role": "user"},
    )

    assert text.startswith("Error:") and "already exists" in text, text
    assert users_on_disk(tmp_path)["new@example.com"]["role"] == "admin"
