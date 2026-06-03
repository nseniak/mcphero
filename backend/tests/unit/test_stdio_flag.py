"""Tests for the ALLOW_STDIO_MCP feature flag."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcpolis.adapters.repositories.upstream_config_loader import (
    build_upstream,
    load_merged_config,
)
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as


MCP_JSON_STDIO = {
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        },
        "github": {"url": "http://localhost:9000/mcp"},
    }
}

CONFIG_JSON = {
    "upstreams": {
        "github": {"display_name": "GitHub", "auth_mode": "service_account"},
    },
    "roles": {
        "admin": {
            "is_admin": True,
            "settings": {"mcp_access": {"auto_enable_new": True}},
        },
    },
    "users": {
        "admin@example.com": {"role": "admin"},
    },
}

def make_test_client(
    tmp_path: Path,
    allow_stdio_mcp: bool = True,
    mcp_json_data: dict[str, Any] | None = None,
) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps(mcp_json_data or MCP_JSON_STDIO))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG_JSON))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # Stdio-flag suite predates the Free/Team plan gates; flip the
    # standalone org to Team so the plan-level seat / MCP-count caps
    # don't compete with the import_confirm tests under audit.
    (data_dir / "subscription.json").write_text(json.dumps({"plan": "team"}))
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
        allow_stdio_mcp=allow_stdio_mcp
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager"
        ".UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry"
        ".ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    login_as(client, "admin@example.com")
    return client


# --- REST API tests ---


def test_stdio_flag_blocks_add_upstream_rest(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, allow_stdio_mcp=False)
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "my-stdio",
            "display_name": "My Stdio",
            "command": "echo",
            "args": ["hello"],
            "auth_mode": "service_account",
        }
    )
    assert resp.status_code == 400
    assert "disabled" in resp.json()["detail"].lower()


def test_stdio_flag_blocks_update_upstream_rest(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, allow_stdio_mcp=False)
    resp = client.put(
        "/api/admin/upstreams/github",
        json={"server_config": {"command": "echo", "args": []}}
    )
    assert resp.status_code == 400
    assert "disabled" in resp.json()["detail"].lower()


def test_stdio_flag_blocks_import_preview(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, allow_stdio_mcp=False)
    import_data = {
        "mcpServers": {
            "new-stdio": {"command": "echo"},
            "new-http": {"url": "http://localhost:9999/mcp"},
        }
    }
    resp = client.post(
        "/api/admin/upstreams/import/preview",
        json={"data": import_data}
    )
    assert resp.status_code == 200
    data = resp.json()
    by_id = {e["original_id"]: e for e in data["entries"]}
    # stdio entry should be marked as blocked
    assert by_id["new-stdio"]["blocked"] is True
    assert by_id["new-stdio"]["blocked_reason"] is not None
    # http entry should not be blocked
    assert by_id["new-http"]["blocked"] is False


def test_stdio_flag_blocks_import_confirm(tmp_path: Path) -> None:
    client = make_test_client(tmp_path, allow_stdio_mcp=False)
    resp = client.post(
        "/api/admin/upstreams/import/confirm",
        json={
            "data": {"mcpServers": {
                "new-stdio": {"command": "echo"},
                "new-http": {"url": "http://localhost:9999/mcp"},
            }},
            "entries": [
                {
                    "scope": "standard", "project_path": None,
                    "original_id": "new-stdio", "target_id": "new-stdio",
                },
                {
                    "scope": "standard", "project_path": None,
                    "original_id": "new-http", "target_id": "new-http",
                },
            ],
        }
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "new-http" in data["added"]
    assert "new-stdio" not in data["added"]
    error_ids = [e["id"] for e in data["errors"]]
    assert "new-stdio" in error_ids


@pytest.mark.asyncio
async def test_stdio_flag_blocks_admin_mcp_tool(tmp_path: Path) -> None:
    # Build the app so config files are seeded, but we call the admin tool
    # directly below rather than hitting it over HTTP.
    make_test_client(tmp_path, allow_stdio_mcp=False)
    # Since the admin MCP controller returns error text (not exception),
    # we test the function directly
    from mcpolis.entrypoints.controllers.admin_mcp_controller import (
        create_admin_mcp_server
    )
    from mcpolis.domain.services.upstream_config_service import UpstreamConfigService
    from mcpolis.domain.services.tool_registry import ToolRegistry
    from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
    from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
    from mcpolis.adapters.repositories.file_config_store import FileConfigStore
    from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
    from mcpolis.adapters.repositories.file_upstream_config_store import FileUpstreamConfigStore
    from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
    from mcpolis.domain.services.policy_engine import PolicyEngine
    from tests.unit.factories import make_runtime_manager

    mcp_json = tmp_path / "mcp2.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config_path = tmp_path / "config2.json"
    config_path.write_text(json.dumps(CONFIG_JSON))

    config_store = FileConfigStore(config_path)
    from mcpolis.domain.ports import DEFAULT_ORG_ID
    app_config = config_store.ensure_defaults_sync(DEFAULT_ORG_ID)
    policy_engine = PolicyEngine(app_config)
    mcp_store = McpJsonStore(mcp_json)
    upstream_store = FileUpstreamConfigStore(mcp_store, config_store)
    client_manager = UpstreamClientManager([])
    tool_registry = ToolRegistry([], client_manager)
    connection_store = FileConnectionStore(tmp_path)
    config_service = UpstreamConfigService(
        upstream_store, client_manager, tool_registry, connection_store
    )
    audit_repo = FileAuditRepository(
        tmp_path / "data2" / "audit.jsonl"
    )

    rm = make_runtime_manager(
        policy_engine,
        tool_registry=tool_registry,
        client_manager=client_manager,
        config_service=config_service
    )
    server = create_admin_mcp_server(
        runtime_manager=rm,
        audit_repo=audit_repo,
        policy_store=config_store,
        allow_stdio_mcp=False
    )

    # Drive the tool against the running test loop. The previous
    # ``asyncio.get_event_loop().run_until_complete(...)`` approach
    # was order-dependent: when an earlier test in the suite had
    # closed the running loop without restoring one, ``get_event_loop``
    # raised "no current event loop." Marking this test ``asyncio``
    # gives pytest-asyncio ownership of the loop, same as every other
    # async test in the suite.
    tools = await server.list_tools()
    add_tool = next(t for t in tools if t.name == "add_upstream")
    assert add_tool is not None
    result: Any = await server.call_tool(
        "add_upstream",
        {
            "mcp_id": "test-stdio",
            "display_name": "Test",
            "transport": "stdio",
            "command": "echo",
        }
    )
    # call_tool returns (list[Content], metadata)
    content_list = result[0]
    result_text = str(content_list[0].text)
    assert "disabled" in result_text.lower()


def test_stdio_flag_blocks_startup_load(tmp_path: Path) -> None:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps(MCP_JSON_STDIO))
    result = load_merged_config(mcp_json, allow_stdio=False)
    # Only the HTTP upstream should be loaded; stdio should be skipped
    assert len(result) == 1
    assert result[0].id == "github"


def test_stdio_flag_default_true_allows_stdio(tmp_path: Path) -> None:
    # Default (allow_stdio_mcp=True) should allow everything
    client = make_test_client(tmp_path, allow_stdio_mcp=True)

    # Startup should load both upstreams
    resp = client.get("/api/admin/upstreams")
    assert resp.status_code == 200
    ids = {u["id"] for u in resp.json()}
    assert "filesystem" in ids
    assert "github" in ids

    # Adding a stdio upstream should succeed
    resp = client.post(
        "/api/admin/upstreams",
        json={
            "id": "new-stdio",
            "display_name": "New Stdio",
            "command": "echo",
            "auth_mode": "service_account",
        }
    )
    assert resp.status_code == 201


def test_features_endpoint(tmp_path: Path) -> None:
    # With stdio disabled
    client = make_test_client(tmp_path, allow_stdio_mcp=False)
    resp = client.get("/api/config/features")
    assert resp.status_code == 200
    assert resp.json() == {
        "allow_stdio_mcp": False,
        "mode": "standalone",
        "sandbox_provider": "local-subprocess",
    }

    # With stdio enabled (default)
    client2 = make_test_client(tmp_path, allow_stdio_mcp=True)
    resp2 = client2.get("/api/config/features")
    assert resp2.status_code == 200
    assert resp2.json() == {
        "allow_stdio_mcp": True,
        "mode": "standalone",
        "sandbox_provider": "local-subprocess",
    }


def test_build_upstream_raises_when_stdio_disabled() -> None:
    import pytest
    with pytest.raises(ValueError, match="Stdio MCP servers are disabled"):
        build_upstream(
            "test",
            {"command": "echo", "args": []},
            {},
            allow_stdio=False
    )


def test_build_upstream_allows_http_when_stdio_disabled() -> None:
    result = build_upstream(
        "test",
        {"url": "http://localhost:9000/mcp"},
        {},
        allow_stdio=False
    )
    assert result.id == "test"
    assert result.http is not None
