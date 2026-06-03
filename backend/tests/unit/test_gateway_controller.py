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
from tests.unit.factories import make_discovered_tool, make_runtime_manager, make_upstream_definition


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
    router = ToolRouter(
        registry, client_manager, audit_service, upstreams,
        policy_engine=PolicyEngine(SettingsConfig()),
    )
    return registry, router, upstreams


@pytest.mark.asyncio
async def test_list_tools_returns_all_when_no_policy(tmp_path: Path) -> None:
    registry, router, _upstreams = make_gateway_components(tmp_path)
    policy_engine = PolicyEngine(SettingsConfig())
    rm = make_runtime_manager(policy_engine, tool_registry=registry, tool_router=router)
    server = create_mcp_server(rm)

    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = cast(Any, await handler(None))
    tools = cast(list[mcp_types.Tool], result.root.tools)
    names = [t.name for t in tools]
    assert "github__create_issue" in names
    assert "slack__send_message" in names


@pytest.mark.asyncio
async def test_call_tool_delegates_to_router(tmp_path: Path) -> None:
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
    assert not call_result.isError
    assert len(call_result.content) > 0
