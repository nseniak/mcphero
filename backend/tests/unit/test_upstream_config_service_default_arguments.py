"""Editing an upstream's stored default arguments takes effect on the
next tool call, without a restart.

The gateway merges the defaults into every call and checks the result
against the role's argument patterns, both from the live tool router's
copy of the upstream. The admin MCP tools that set or remove defaults
go through ``UpstreamConfigService``; if the service only saved the
change, the gateway would keep sending (and checking) the old defaults.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.repositories.file_upstream_config_store import (
    FileUpstreamConfigStore,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.services.upstream_config_service import UpstreamConfigService


async def make_service_and_router(
    tmp_path: Path,
) -> tuple[UpstreamConfigService, ToolRouter]:
    """A ``github`` upstream whose ``create_issue`` default title is
    "old", wired the way an org runtime wires it."""
    (tmp_path / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"github": {"url": "http://localhost:9000/mcp"}}}))
    (tmp_path / "config.json").write_text(json.dumps({
        "upstreams": {"github": {
            "display_name": "GitHub", "auth_mode": "service_account",
            "default_arguments": {"create_issue": {"title": "old"}},
        }},
        "roles": {"admin": {"is_admin": True}}, "users": {},
    }))
    config_store = FileConfigStore(tmp_path / "config.json")
    upstream_store = FileUpstreamConfigStore(
        McpJsonStore(tmp_path / "mcp.json"), config_store,
    )
    upstreams = await upstream_store.get_all(DEFAULT_ORG_ID)
    client_manager = UpstreamClientManager(upstreams)
    registry = ToolRegistry(upstreams, client_manager)
    router = ToolRouter(
        registry, client_manager, FileAuditRepository(tmp_path / "audit.jsonl"),
        upstreams, policy_engine=PolicyEngine(SettingsConfig()),
    )
    service = UpstreamConfigService(
        upstream_store, client_manager, registry, FileConnectionStore(tmp_path),
        tool_router=router,
        config_repo=config_store,
        policy_engine=PolicyEngine(SettingsConfig()),
    )
    assert router.effective_arguments("github", "create_issue", {}) == {
        "title": "old",
    }
    return service, router


@pytest.mark.asyncio
async def test_set_default_arguments_reaches_the_live_router(tmp_path: Path) -> None:
    service, router = await make_service_and_router(tmp_path)

    await service.set_default_arguments(
        DEFAULT_ORG_ID, "github", "create_issue", {"title": "new"})

    stored = await service.get_upstream(DEFAULT_ORG_ID, "github")
    assert stored is not None
    assert stored.default_arguments["create_issue"] == {"title": "new"}
    assert router.effective_arguments("github", "create_issue", {}) == {
        "title": "new",
    }


@pytest.mark.asyncio
async def test_remove_default_arguments_reaches_the_live_router(
    tmp_path: Path,
) -> None:
    service, router = await make_service_and_router(tmp_path)

    await service.remove_default_arguments(DEFAULT_ORG_ID, "github", "create_issue")

    assert router.effective_arguments("github", "create_issue", {"title": "x"}) == {
        "title": "x",
    }
