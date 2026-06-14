from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import mcp.types as mcp_types
import pytest

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
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
