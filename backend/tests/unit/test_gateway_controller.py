from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import mcp.types as mcp_types
import pytest

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.service_token import (
    SCOPE_ORG_PREFIX,
    SCOPE_ROLE_PREFIX,
    SCOPE_SVC,
    service_identity,
)
from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.entrypoints.controllers.gateway_controller import (
    create_mcp_server,
)
from tests.unit.factories import (
    make_discovered_tool,
    make_full_access_config,
    make_runtime_manager,
    make_upstream_definition,
)


def make_gateway_components(
    tmp_path: Path,
) -> tuple[ToolRegistry, ToolRouter, list[Any]]:
    gh = make_upstream_definition(id="github")
    sl = make_upstream_definition(id="slack")
    upstreams = [gh, sl]

    client_manager = UpstreamClientManager(upstreams)
    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="done")],
            isError=False,
        )
    )
    from tests.unit._state_seed import seed_shared_session
    seed_shared_session(client_manager, "github", session=mock_session)
    seed_shared_session(client_manager, "slack", session=mock_session)

    audit_service = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry(upstreams, client_manager)
    registry._tools = [
        make_discovered_tool(upstream_id="github", original_name="create_issue"),
        make_discovered_tool(upstream_id="slack", original_name="send_message"),
    ]
    # The unauthenticated handler path resolves the user to
    # "anonymous"; seed it with a role so plumbing tests see tools.
    policy_engine = PolicyEngine(
        make_full_access_config(["github", "slack"], ["anonymous"]),
    )
    router = ToolRouter(
        registry, client_manager, audit_service, upstreams,
        policy_engine=policy_engine,
    )
    return registry, router, upstreams


@pytest.mark.asyncio
async def test_list_tools_returns_all_for_full_access_role(tmp_path: Path) -> None:
    registry, router, _upstreams = make_gateway_components(tmp_path)
    policy_engine = PolicyEngine(
        make_full_access_config(["github", "slack"], ["anonymous"]),
    )
    rm = make_runtime_manager(policy_engine, tool_registry=registry, tool_router=router)
    server = create_mcp_server(rm)

    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = cast(Any, await handler(None))
    tools = cast(list[mcp_types.Tool], result.root.tools)
    names = [t.name for t in tools]
    assert "github__create_issue" in names
    assert "slack__send_message" in names


@pytest.mark.asyncio
async def test_list_tools_empty_when_org_has_zero_roles(tmp_path: Path) -> None:
    """Zero-roles org fails closed at the gateway: no tools for any
    identity, member or not (no permissive fallback)."""
    registry, router, _upstreams = make_gateway_components(tmp_path)
    policy_engine = PolicyEngine(SettingsConfig())
    rm = make_runtime_manager(policy_engine, tool_registry=registry, tool_router=router)
    server = create_mcp_server(rm)

    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = cast(Any, await handler(None))
    tools = cast(list[mcp_types.Tool], result.root.tools)
    assert tools == []


@pytest.mark.asyncio
async def test_call_tool_denied_when_org_has_zero_roles(tmp_path: Path) -> None:
    """Tool calls against a zero-roles org are denied the same way a
    role-less user in a roled org is denied (the handler folds the
    denial into a text content block)."""
    registry, router, _upstreams = make_gateway_components(tmp_path)
    policy_engine = PolicyEngine(SettingsConfig())
    rm = make_runtime_manager(policy_engine, tool_registry=registry, tool_router=router)
    server = create_mcp_server(rm)

    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(
            name="github__create_issue",
            arguments={"title": "Test"},
        ),
    )
    result = cast(Any, await handler(request))
    call_result = cast(mcp_types.CallToolResult, result.root)
    text = cast(mcp_types.TextContent, call_result.content[0]).text
    assert "Access denied" in text


@pytest.mark.asyncio
async def test_call_tool_delegates_to_router(tmp_path: Path) -> None:
    registry, router, _upstreams = make_gateway_components(tmp_path)
    policy_engine = PolicyEngine(
        make_full_access_config(["github", "slack"], ["anonymous"]),
    )
    rm = make_runtime_manager(policy_engine, tool_registry=registry, tool_router=router)
    server = create_mcp_server(rm)

    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(
            name="github__create_issue",
            arguments={"title": "Test"},
        ),
    )
    result = cast(Any, await handler(request))
    call_result = cast(mcp_types.CallToolResult, result.root)
    assert not call_result.isError
    # Assert the routed result's payload, not just "some content":
    # a policy denial also produces a text block, and this test must
    # fail if the call stops reaching the mocked upstream session.
    text = cast(mcp_types.TextContent, call_result.content[0]).text
    assert text == "done"


def _mcp_disabled_config() -> SettingsConfig:
    """Role with ``github`` enabled but ``slack`` left off the access map.

    Calling a ``slack`` tool exercises the MCP-disabled deny path in
    ``_check_upstream_and_tool_policy``.
    """
    return SettingsConfig(
        roles={
            "default": RoleDefinition(
                is_default=True,
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={"github": True}),
                ),
            ),
        },
        users={"anonymous": UserDefinition(role="default")},
    )


def _forbidden_arg_config() -> SettingsConfig:
    """Role granting ``github`` access but forbidding the substring
    ``blocked`` in ``create_issue``'s ``title`` argument."""
    return SettingsConfig(
        roles={
            "default": RoleDefinition(
                is_default=True,
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={"github": True}),
                    argument_constraints={
                        "github__create_issue": {
                            "title": ArgumentConstraint(
                                pattern="blocked", mode="forbid",
                            ),
                        },
                    },
                ),
            ),
        },
        users={"anonymous": UserDefinition(role="default")},
    )


async def _call(
    handler: Any, name: str, arguments: dict[str, Any],
) -> mcp_types.CallToolResult:
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(request)
    return cast(mcp_types.CallToolResult, result.root)


@pytest.mark.asyncio
async def test_denied_mcp_disabled_writes_audit_entry(tmp_path: Path) -> None:
    """An MCP-disabled denial writes exactly one ``denied`` audit row
    carrying the tool, upstream, acting user, and a readable reason."""
    registry, router, _ = make_gateway_components(tmp_path)
    rm = make_runtime_manager(
        PolicyEngine(_mcp_disabled_config()),
        tool_registry=registry, tool_router=router,
    )
    server = create_mcp_server(rm)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    call_result = await _call(handler, "slack__send_message", {"text": "hi"})
    assert call_result.isError
    text = cast(mcp_types.TextContent, call_result.content[0]).text
    assert "disabled" in text

    entries = await router._audit.search("default", limit=100)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "tool_call"
    assert entry["policy_decision"] == "denied"
    assert entry["user_id"] == "anonymous"
    assert entry["upstream_id"] == "slack"
    assert entry["tool"] == "slack__send_message"
    # Reason names which MCP was disabled, for the operator.
    reason = (entry.get("error_message") or "") + (entry.get("policy_rule") or "")
    assert "slack" in reason and "disabled" in reason


@pytest.mark.asyncio
async def test_denied_forbidden_argument_writes_audit_entry(
    tmp_path: Path,
) -> None:
    """An argument-check (Forbid) denial writes one ``denied`` audit row
    naming the offending argument."""
    registry, router, _ = make_gateway_components(tmp_path)
    rm = make_runtime_manager(
        PolicyEngine(_forbidden_arg_config()),
        tool_registry=registry, tool_router=router,
    )
    server = create_mcp_server(rm)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    call_result = await _call(
        handler, "github__create_issue", {"title": "this should be blocked"},
    )
    assert call_result.isError
    text = cast(mcp_types.TextContent, call_result.content[0]).text
    assert "forbidden pattern" in text

    entries = await router._audit.search("default", limit=100)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["policy_decision"] == "denied"
    assert entry["upstream_id"] == "github"
    assert entry["tool"] == "github__create_issue"
    reason = (entry.get("error_message") or "") + (entry.get("policy_rule") or "")
    assert "title" in reason


@pytest.mark.asyncio
async def test_allowed_call_writes_single_audit_entry(tmp_path: Path) -> None:
    """Allowed calls still produce exactly one ``allowed`` row — the
    denial-audit change must not double-log the happy path."""
    registry, router, _ = make_gateway_components(tmp_path)
    rm = make_runtime_manager(
        PolicyEngine(make_full_access_config(["github", "slack"], ["anonymous"])),
        tool_registry=registry, tool_router=router,
    )
    server = create_mcp_server(rm)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    call_result = await _call(handler, "github__create_issue", {"title": "ok"})
    assert not call_result.isError

    entries = await router._audit.search("default", limit=100)
    assert len(entries) == 1
    assert entries[0]["policy_decision"] == "allowed"


# ---------------------------------------------------------------------------
# EXPO-1 / EXPO-2 — exposure under a service-token boundary role.
#
# A service token's ``svc:<label>`` identity has no ``config.users`` entry
# by design; its role is carried in the auth scopes
# (``[SCOPE_SVC, mcpolis:role:<role>, mcpolis:org:<org>]``) and read back
# by ``_get_boundary_role`` → handed to the PolicyEngine. These tests pin
# that the gateway controller honors that boundary role on every exposure
# surface — tools/list, tools/call (allowed + denied), and the bare-name /
# bare-URI fallbacks — and audits the call under the svc identity.
# ---------------------------------------------------------------------------

SVC_LABEL = "expo-bot"
SVC_IDENTITY = service_identity(SVC_LABEL)


def _svc_scoped_config() -> SettingsConfig:
    """Two roles, no svc user entry: ``reader`` grants only ``github``;
    ``none`` grants nothing. The boundary role decides exposure."""
    return SettingsConfig(
        roles={
            "reader": RoleDefinition(
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={"github": True}),
                ),
            ),
            "none": RoleDefinition(
                is_default=True,
                settings=RoleSettings(mcp_access=McpAccessConfig(mcps={})),
            ),
        },
        users={},
    )


def _set_svc_auth(role: str = "reader", org: str = "default") -> Any:
    """Set request-scoped auth context for the service-token identity,
    carrying the boundary role in the SDK ``AccessToken.scopes`` channel."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    auth_user = AuthenticatedUser(
        AccessToken(
            token="svct_fake",
            client_id=SVC_IDENTITY,  # → AuthenticatedUser.display_name
            scopes=[
                SCOPE_SVC,
                SCOPE_ROLE_PREFIX + role,
                SCOPE_ORG_PREFIX + org,
            ],
            expires_at=int(time.time()) + 3600,
        )
    )
    return auth_context_var.set(auth_user)


def _reset_auth(token: Any) -> None:
    from mcp.server.auth.middleware.auth_context import auth_context_var
    auth_context_var.reset(token)


@pytest.mark.asyncio
async def test_list_tools_filtered_to_boundary_role(tmp_path: Path) -> None:
    """tools/list under a service-token ``reader`` boundary role is
    filtered to that role's upstream (``github``), even though the svc
    identity is absent from ``config.users``."""
    registry, router, _ = make_gateway_components(tmp_path)
    rm = make_runtime_manager(
        PolicyEngine(_svc_scoped_config()),
        tool_registry=registry, tool_router=router,
    )
    server = create_mcp_server(rm)
    handler = server.request_handlers[mcp_types.ListToolsRequest]

    auth_token = _set_svc_auth(role="reader")
    try:
        result = cast(Any, await handler(None))
    finally:
        _reset_auth(auth_token)
    names = [t.name for t in cast(list[mcp_types.Tool], result.root.tools)]
    assert names == ["github__create_issue"]


@pytest.mark.asyncio
async def test_call_tool_allowed_under_boundary_role_audits_svc_identity(
    tmp_path: Path,
) -> None:
    """An allowed call under the ``reader`` boundary role reaches the
    routed upstream and writes a single ``allowed`` audit row keyed to
    the ``svc:<label>`` identity."""
    registry, router, _ = make_gateway_components(tmp_path)
    rm = make_runtime_manager(
        PolicyEngine(_svc_scoped_config()),
        tool_registry=registry, tool_router=router,
    )
    server = create_mcp_server(rm)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    auth_token = _set_svc_auth(role="reader")
    try:
        call_result = await _call(
            handler, "github__create_issue", {"title": "ok"},
        )
    finally:
        _reset_auth(auth_token)
    assert not call_result.isError
    text = cast(mcp_types.TextContent, call_result.content[0]).text
    assert text == "done"

    entries = await router._audit.search("default", limit=100)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["policy_decision"] == "allowed"
    assert entry["user_id"] == SVC_IDENTITY
    assert entry["tool"] == "github__create_issue"


@pytest.mark.asyncio
async def test_call_tool_denied_under_boundary_role_audits_svc_identity(
    tmp_path: Path,
) -> None:
    """A call to an upstream outside the ``reader`` boundary role
    (``slack``) is denied and writes a ``denied`` audit row under the
    ``svc:<label>`` identity."""
    registry, router, _ = make_gateway_components(tmp_path)
    rm = make_runtime_manager(
        PolicyEngine(_svc_scoped_config()),
        tool_registry=registry, tool_router=router,
    )
    server = create_mcp_server(rm)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    auth_token = _set_svc_auth(role="reader")
    try:
        call_result = await _call(
            handler, "slack__send_message", {"text": "hi"},
        )
    finally:
        _reset_auth(auth_token)
    assert call_result.isError
    text = cast(mcp_types.TextContent, call_result.content[0]).text
    assert "Access denied" in text

    entries = await router._audit.search("default", limit=100)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["policy_decision"] == "denied"
    assert entry["user_id"] == SVC_IDENTITY
    assert entry["upstream_id"] == "slack"
    assert entry["tool"] == "slack__send_message"


@pytest.mark.asyncio
async def test_bare_tool_name_resolution_honors_boundary_role(
    tmp_path: Path,
) -> None:
    """EXPO-2: a bare tool name (MCP-Apps widget callback shape) resolves
    only against the boundary role's allowed upstreams.

    ``create_issue`` (owned by the allowed ``github``) resolves and the
    call goes through; ``send_message`` (owned by the disallowed
    ``slack``) is unknown to the boundary role, so the bare-name resolver
    reports it as unknown rather than reaching ``slack``."""
    registry, router, _ = make_gateway_components(tmp_path)
    rm = make_runtime_manager(
        PolicyEngine(_svc_scoped_config()),
        tool_registry=registry, tool_router=router,
    )
    server = create_mcp_server(rm)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    auth_token = _set_svc_auth(role="reader")
    try:
        allowed = await _call(handler, "create_issue", {"title": "ok"})
        denied = await _call(handler, "send_message", {"text": "hi"})
    finally:
        _reset_auth(auth_token)

    # Allowed upstream's bare name resolves and routes through.
    assert not allowed.isError
    assert cast(mcp_types.TextContent, allowed.content[0]).text == "done"

    # Disallowed upstream's bare name is invisible to the boundary role:
    # the resolver can't see ``slack``, so it reports the tool unknown.
    assert denied.isError
    denied_text = cast(mcp_types.TextContent, denied.content[0]).text
    assert "Unknown tool 'send_message'" in denied_text
