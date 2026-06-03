"""End-to-end test: fake upstream MCP server → MCPolis gateway → MCP client."""
# NOTE: no `from __future__ import annotations` — FastMCP tool registration
# uses issubclass() on annotations which breaks with stringified annotations.

import asyncio
import json
from pathlib import Path
from typing import cast

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings


def _write_config(tmp_path: Path, upstream_port: int) -> tuple[Path, Path]:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "fake": {"url": f"http://127.0.0.1:{upstream_port}/mcp"}
        }
    }))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "upstreams": {
            "fake": {
                "display_name": "Fake MCP",
                "auth_mode": "service_account",
                "default_arguments": {"greet": {"suffix": "!"}},
            }
        },
        "roles": {
            "admin": {
                "is_admin": True,
                "settings": {
                    "mcp_access": {"auto_enable_new": True, "mcps": {"fake": True}},
                },
            },
            "limited": {
                "settings": {
                    "mcp_access": {"mcps": {"fake": True}},
                    "tool_access": {"fake": {"tools": {"greet": True}}},
                    "argument_constraints": {
                        "fake__greet": {"name": {"pattern": "^World$", "mode": "allow"}},
                    },
                },
            },
        },
        "users": {
            "admin@test.com": {"role": "admin"},
            "limited@test.com": {"role": "limited"},
        },
    }))
    return mcp_json, config


def _create_fake_upstream() -> FastMCP:
    """Create a simple MCP server with one tool."""
    server = FastMCP(name="FakeUpstream")

    @server.tool(name="greet", description="Say hello")
    def greet(name: str, suffix: str = "") -> str:  # pyright: ignore[reportUnusedFunction]
        return f"Hello, {name}{suffix}"

    return server


@pytest.mark.asyncio
async def test_end_to_end_tool_discovery_and_call(tmp_path: Path) -> None:
    # 1. Start fake upstream MCP server
    fake_mcp = _create_fake_upstream()
    upstream_app = fake_mcp.streamable_http_app()

    upstream_port = 19876
    # ``ws="none"`` skips loading uvicorn's ws adapter, which still
    # imports the deprecated ``websockets.server.WebSocketServerProtocol``
    # path and emits a DeprecationWarning. The MCP gateway uses
    # streamable-HTTP+SSE only — no WebSocket support is needed.
    upstream_config = uvicorn.Config(
        upstream_app, host="127.0.0.1", port=upstream_port,
        log_level="warning", ws="none",
    )
    upstream_server = uvicorn.Server(upstream_config)

    # 2. Create and start gateway
    mcp_json_path, config_path = _write_config(tmp_path, upstream_port)
    # Override config with explicit admin access for the e2e user
    # (gateway requires a real bearer token now — minted below via the
    # test_mode mint_test_token endpoint).
    config_path.write_text(json.dumps({
        "upstreams": {
            "fake": {
                "display_name": "Fake MCP",
                "auth_mode": "service_account",
                "default_arguments": {"greet": {"suffix": "!"}},
            }
        },
        "roles": {
            "admin": {
                "is_admin": True,
                "settings": {
                    "mcp_access": {"auto_enable_new": True, "mcps": {"fake": True}},
                },
            },
        },
        "users": {"admin@test.com": {"role": "admin"}},
    }))
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        host="127.0.0.1",
        port=19877,
        mcp_json_path=mcp_json_path,
        config_path=config_path,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        test_mode=True,
        google_client_id="",
        google_client_secret="",
        session_secret="integration-test-secret",
        server_url="http://127.0.0.1:19877",
    )
    gateway_app = create_app(settings)
    gateway_config = uvicorn.Config(
        gateway_app, host="127.0.0.1", port=19877,
        log_level="warning", ws="none",
    )
    gateway_server = uvicorn.Server(gateway_config)

    async def run_servers() -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(upstream_server.serve())
            tg.create_task(gateway_server.serve())

    server_task = asyncio.create_task(run_servers())

    try:
        # Wait for both servers to be ready
        for port in (upstream_port, 19877):
            for _ in range(50):
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(f"http://127.0.0.1:{port}/health")
                        if resp.status_code == 200:
                            break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.1)

        # 3. Connect MCP client to gateway with a real bearer token.
        gateway_provider = gateway_app.state.mcp_gateway_oauth_provider
        token = await gateway_provider.mint_test_token("admin@test.com")
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
        ) as http_client, streamable_http_client(
            "http://127.0.0.1:19877/mcp/", http_client=http_client,
        ) as (read_stream, write_stream, _get_session_id):
            session = ClientSession(read_stream, write_stream)
            async with session:
                await session.initialize()

                # 4. Verify tools/list returns prefixed tools
                tools_result = await session.list_tools()
                tool_names = [t.name for t in tools_result.tools]
                assert "fake__greet" in tool_names

                greet_tool = next(t for t in tools_result.tools if t.name == "fake__greet")
                assert greet_tool.description == "Say hello"

                # 5. Verify tools/call proxies correctly (with default_arguments merged)
                call_result = await session.call_tool("fake__greet", {"name": "World"})
                assert not call_result.isError
                # The default_arguments adds suffix="!"
                text = call_result.content[0].text  # type: ignore[union-attr]
                assert "Hello, World!" in text

        # 6. Verify audit log
        audit_path = tmp_path / "data" / "audit.jsonl"
        assert audit_path.exists()
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) >= 2
        entries = [json.loads(line) for line in lines]
        # First entry should be client_connect
        assert entries[0]["action"] == "client_connect"
        assert entries[0].get("client_type") is not None
        # Second entry should be the tool call
        assert entries[1]["tool"] == "fake__greet"
        assert entries[1]["response_status"] == "success"

    finally:
        upstream_server.should_exit = True
        gateway_server.should_exit = True
        # Give servers time to shut down
        await asyncio.sleep(0.3)
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, ExceptionGroup):
            pass


async def _start_gateway_with_policy(
    tmp_path: Path, upstream_port: int, gateway_port: int
) -> tuple[uvicorn.Server, uvicorn.Server, asyncio.Task[None], object]:
    """Start fake upstream + gateway with policy config. Returns servers, task, and gateway provider."""
    fake_mcp = _create_fake_upstream()
    upstream_app = fake_mcp.streamable_http_app()
    upstream_server = uvicorn.Server(
        uvicorn.Config(
            upstream_app, host="127.0.0.1", port=upstream_port,
            log_level="warning", ws="none",
        )
    )

    mcp_json_path, config_path = _write_config(tmp_path, upstream_port)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        host="127.0.0.1",
        port=gateway_port,
        mcp_json_path=mcp_json_path,
        config_path=config_path,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        test_mode=True,
        google_client_id="",
        google_client_secret="",
        session_secret="integration-test-secret",
        server_url=f"http://127.0.0.1:{gateway_port}",
    )
    gateway_app = create_app(settings)
    gateway_server = uvicorn.Server(
        uvicorn.Config(
            gateway_app, host="127.0.0.1", port=gateway_port,
            log_level="warning", ws="none",
        )
    )

    async def run_servers() -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(upstream_server.serve())
            tg.create_task(gateway_server.serve())

    server_task = asyncio.create_task(run_servers())

    # Wait for both servers to be ready
    for port in (upstream_port, gateway_port):
        for _ in range(50):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/health")
                    if resp.status_code == 200:
                        break
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.1)

    return upstream_server, gateway_server, server_task, gateway_app.state.mcp_gateway_oauth_provider


async def _connect_as(
    gateway_port: int, gateway_provider: object, user_id: str,
) -> list[str]:
    """Connect to gateway as a user and return tool names."""
    token = await gateway_provider.mint_test_token(user_id)  # type: ignore[attr-defined]
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
    ) as http_client:
        async with streamable_http_client(
            f"http://127.0.0.1:{gateway_port}/mcp/", http_client=http_client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [t.name for t in result.tools]


async def _call_tool_as(
    gateway_port: int,
    gateway_provider: object,
    user_id: str,
    tool_name: str,
    arguments: dict[str, str],
) -> tuple[bool, str]:
    """Call a tool as a user and return (is_error, text)."""
    token = await gateway_provider.mint_test_token(user_id)  # type: ignore[attr-defined]
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
    ) as http_client:
        async with streamable_http_client(
            f"http://127.0.0.1:{gateway_port}/mcp/", http_client=http_client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                text = cast(str, result.content[0].text)  # type: ignore[union-attr]
                return result.isError or False, text


@pytest.mark.asyncio
async def test_policy_admin_sees_all_tools(tmp_path: Path) -> None:
    upstream_server, gateway_server, server_task, gateway_provider = await _start_gateway_with_policy(
        tmp_path, 19878, 19879
    )
    try:
        tools = await _connect_as(19879, gateway_provider, "admin@test.com")
        assert "fake__greet" in tools
    finally:
        upstream_server.should_exit = True
        gateway_server.should_exit = True
        await asyncio.sleep(0.3)
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, ExceptionGroup):
            pass


@pytest.mark.asyncio
async def test_policy_limited_user_sees_only_allowed_tools(tmp_path: Path) -> None:
    upstream_server, gateway_server, server_task, gateway_provider = await _start_gateway_with_policy(
        tmp_path, 19880, 19881
    )
    try:
        tools = await _connect_as(19881, gateway_provider, "limited@test.com")
        assert "fake__greet" in tools
    finally:
        upstream_server.should_exit = True
        gateway_server.should_exit = True
        await asyncio.sleep(0.3)
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, ExceptionGroup):
            pass


@pytest.mark.asyncio
async def test_policy_unknown_user_sees_no_tools(tmp_path: Path) -> None:
    upstream_server, gateway_server, server_task, gateway_provider = await _start_gateway_with_policy(
        tmp_path, 19882, 19883
    )
    try:
        tools = await _connect_as(19883, gateway_provider, "nobody@test.com")
        assert tools == []
    finally:
        upstream_server.should_exit = True
        gateway_server.should_exit = True
        await asyncio.sleep(0.3)
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, ExceptionGroup):
            pass


@pytest.mark.asyncio
async def test_policy_argument_pattern_blocks_call(tmp_path: Path) -> None:
    upstream_server, gateway_server, server_task, gateway_provider = await _start_gateway_with_policy(
        tmp_path, 19884, 19885
    )
    try:
        # Allowed call
        _is_error, text = await _call_tool_as(
            19885, gateway_provider, "limited@test.com", "fake__greet", {"name": "World"}
        )
        assert "Hello, World" in text

        # Blocked by allow pattern (doesn't match ^World$)
        _is_error, text = await _call_tool_as(
            19885, gateway_provider, "limited@test.com", "fake__greet", {"name": "evil"}
        )
        assert "Access denied" in text
    finally:
        upstream_server.should_exit = True
        gateway_server.should_exit = True
        await asyncio.sleep(0.3)
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, ExceptionGroup):
            pass
