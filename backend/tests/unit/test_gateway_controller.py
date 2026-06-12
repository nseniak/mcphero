from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import mcp.types as mcp_types
import pytest

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.settings import SettingsConfig
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
