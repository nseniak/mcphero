"""Full-app gateway tests for service tokens (standalone mode).

Spins up a fake upstream + the real gateway on loopback ports (same
harness as ``test_mcp_integration.py``) and connects with
``svct_``-prefixed bearers minted into the same registry file the
app reads. Pins:

- tools/list reflects the token's role (not ``config.users``);
- a no-access role gets zero tools and denied calls;
- a deleted role fails closed;
- revocation bites on the next connection;
- the audit log records the ``svc:<label>`` identity.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings

# Fixed-port convention shared with test_mcp_integration.py (which
# owns 19876-19885): run-unit-tests.sh uses ``--dist loadfile``, so a
# file's tests run serially in one worker — fixed ports are safe as
# long as every file claims a disjoint range. Next free range starts
# at 19888.
GATEWAY_PORT = 19886
UPSTREAM_PORT = 19887


def _write_config(tmp_path: Path, upstream_port: int) -> tuple[Path, Path]:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "fake": {"url": f"http://127.0.0.1:{upstream_port}/mcp"},
        },
    }))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "upstreams": {
            "fake": {"display_name": "Fake", "auth_mode": "service_account"},
        },
        "roles": {
            "admin": {
                "is_admin": True,
                "settings": {"mcp_access": {"mcps": {"fake": True}}},
            },
            "full": {
                "settings": {"mcp_access": {"mcps": {"fake": True}}},
            },
            "none": {
                "settings": {"mcp_access": {"mcps": {}}},
            },
        },
        "users": {
            "admin@example.com": {"role": "admin"},
        },
    }))
    return mcp_json, config


def _create_fake_upstream():  # noqa: ANN202 — FastMCP type lives in test dep
    from mcp.server.fastmcp import FastMCP

    fake = FastMCP("fake", host="127.0.0.1", port=UPSTREAM_PORT)

    @fake.tool(description="Say hello")
    def greet(name: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return f"Hello, {name}"

    return fake


async def _start_stack(
    tmp_path: Path,
) -> tuple[uvicorn.Server, uvicorn.Server, asyncio.Task[None]]:
    fake_mcp = _create_fake_upstream()
    upstream_server = uvicorn.Server(uvicorn.Config(
        fake_mcp.streamable_http_app(), host="127.0.0.1",
        port=UPSTREAM_PORT, log_level="warning", ws="none",
    ))

    mcp_json_path, config_path = _write_config(tmp_path, UPSTREAM_PORT)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        host="127.0.0.1",
        port=GATEWAY_PORT,
        mcp_json_path=mcp_json_path,
        config_path=config_path,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        test_mode=True,
        google_client_id="",
        google_client_secret="",
        session_secret="svc-token-test-secret",
        server_url=f"http://127.0.0.1:{GATEWAY_PORT}",
    )
    gateway_server = uvicorn.Server(uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=GATEWAY_PORT,
        log_level="warning", ws="none",
    ))

    async def run_servers() -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(upstream_server.serve())
            tg.create_task(gateway_server.serve())

    server_task = asyncio.create_task(run_servers())
    for port in (UPSTREAM_PORT, GATEWAY_PORT):
        for _ in range(50):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/health")
                    if resp.status_code == 200:
                        break
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.1)
    return upstream_server, gateway_server, server_task


async def _stop_stack(
    upstream_server: uvicorn.Server,
    gateway_server: uvicorn.Server,
    server_task: asyncio.Task[None],
) -> None:
    upstream_server.should_exit = True
    gateway_server.should_exit = True
    await asyncio.sleep(0.3)
    server_task.cancel()
    try:
        await server_task
    except (asyncio.CancelledError, ExceptionGroup):
        pass


def make_registry_service(tmp_path: Path) -> ServiceTokenService:
    """Sibling service over the same registry file the app reads.

    ``FileServiceTokenRepository`` re-reads the JSON on every call, so
    tokens minted here are immediately visible to the running gateway
    — the verify path under test is the app's own.
    """
    return ServiceTokenService(
        repo=FileServiceTokenRepository(tmp_path / "data"),
    )


async def _list_tools_with(token: str) -> list[str]:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
    ) as http_client:
        async with streamable_http_client(
            f"http://127.0.0.1:{GATEWAY_PORT}/mcp/", http_client=http_client,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [t.name for t in result.tools]


async def _call_tool_with(
    token: str, tool_name: str, arguments: dict[str, str],
) -> tuple[bool, str]:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
    ) as http_client:
        async with streamable_http_client(
            f"http://127.0.0.1:{GATEWAY_PORT}/mcp/", http_client=http_client,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                text = cast(str, result.content[0].text)  # type: ignore[union-attr]
                return result.isError or False, text


@pytest.mark.asyncio
async def test_service_token_gateway_end_to_end(tmp_path: Path) -> None:
    """One stack boot, every gateway-side service-token behavior."""
    upstream_server, gateway_server, server_task = await _start_stack(tmp_path)
    registry = make_registry_service(tmp_path)
    try:
        full = await registry.mint(
            org_id="default", label="full-bot", role_name="full",
            created_by="admin@example.com",
        )
        none = await registry.mint(
            org_id="default", label="none-bot", role_name="none",
            created_by="admin@example.com",
        )
        ghost = await registry.mint(
            org_id="default", label="ghost-bot", role_name="deleted-role",
            created_by="admin@example.com",
        )

        # Role-driven discovery: svc identity is not in config.users.
        assert await _list_tools_with(full.raw_token) == ["fake__greet"]

        # Allowed call goes through and is audited under svc:<label>.
        # NOTE: assert on text, not isError — the SDK drops the
        # handler's isError flag on the wire (same convention as
        # test_mcp_integration.py's policy tests).
        _is_error, text = await _call_tool_with(
            full.raw_token, "fake__greet", {"name": "World"},
        )
        assert "Hello, World" in text
        assert "Access denied" not in text

        # No-access role: zero tools, denied call.
        assert await _list_tools_with(none.raw_token) == []
        _is_error, text = await _call_tool_with(
            none.raw_token, "fake__greet", {"name": "World"},
        )
        assert "Access denied" in text

        # Deleted role fails closed (token authenticates, no access).
        assert await _list_tools_with(ghost.raw_token) == []

        # Unknown / revoked tokens never authenticate.
        with pytest.raises(BaseException):
            await _list_tools_with("svct_never-minted")
        await registry.revoke("default", "full-bot")
        with pytest.raises(BaseException):
            await _list_tools_with(full.raw_token)

        # Audit rows carry the svc identity.
        audit_path = tmp_path / "data" / "audit.jsonl"
        entries = [
            json.loads(line)
            for line in audit_path.read_text().strip().splitlines()
        ]
        tool_calls = [e for e in entries if e.get("tool") == "fake__greet"]
        assert any(
            e["user_id"] == "svc:full-bot"
            and e["response_status"] == "success"
            for e in tool_calls
        )
        connects = [e for e in entries if e.get("action") == "client_connect"]
        assert any(e["user_id"] == "svc:full-bot" for e in connects)
    finally:
        await _stop_stack(upstream_server, gateway_server, server_task)
