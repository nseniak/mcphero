"""Real-transport stall recovery: the 2026-06-12 prod incident, replayed
against a REAL streamable-HTTP server and a REAL UpstreamClientManager
session — no mocked transports.

Sentry MCPOLIS-BACKEND-R/-S: a per-user OAuth session's HTTP connection
died during an idle gap; the SDK task group exited and closed the
in-memory streams, but the dead ClientSession stayed cached, so the
stall retry was handed the same dead session and every call failed with
``anyio.ClosedResourceError`` until the idle sweep.

The mocked unit tests in test_tool_router_oauth.py pin the eviction
contract; this test pins the links mocks can't prove:

- a genuinely dead HTTP transport raises something
  ``is_transport_stall`` actually classifies as a stall;
- evicting the dead session runs the real ``ConnectionTask.close()``
  (already-exited task) without hanging;
- the in-call retry lands on a genuinely fresh transport and returns
  the real tool result.

Only the OAuth token exchange is substituted (the patched
``reconnect_with_stored_tokens`` reconnects with a bearer instead of
walking the token-refresh dance) — that seam is covered end-to-end by
the e2e OAuth specs (16-per-user-oauth, 18a-token-refresh-silent).
"""
# NOTE: no `from __future__ import annotations` — FastMCP tool registration
# uses issubclass() on annotations which breaks with stringified annotations.

import asyncio
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from unittest.mock import AsyncMock

from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import ToolAnnotations
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services import upstream_connection_service as ucs_module
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from tests.unit.factories import (
    make_discovered_tool,
    make_upstream_definition,
)

USER = "alice@co.com"
UPSTREAM_ID = "flaky"


def test_is_transport_stall_classification() -> None:
    """Pin the stall/normal-error boundary. Stalls reconnect; normal
    server answers must not (eviction on every error would turn each
    upstream hiccup into a reconnect storm)."""
    import anyio
    import mcp.types as mcp_types
    from mcp.shared.exceptions import McpError

    from mcpolis.domain.services.tool_registry import is_transport_stall

    def mcp_error(code: int, message: str) -> McpError:
        return McpError(mcp_types.ErrorData(code=code, message=message))

    # Transport gone — stall.
    assert is_transport_stall(asyncio.TimeoutError())
    assert is_transport_stall(anyio.ClosedResourceError())
    assert is_transport_stall(anyio.BrokenResourceError())
    assert is_transport_stall(
        mcp_error(mcp_types.CONNECTION_CLOSED, "Connection closed")
    )
    # Session id no longer honored (upstream restarted) — stall. The
    # SDK hardcodes positive 32600; the negative spelling is accepted
    # defensively.
    assert is_transport_stall(mcp_error(32600, "Session terminated"))
    assert is_transport_stall(mcp_error(-32600, "Session terminated"))
    # Normal server answers — not stalls.
    assert not is_transport_stall(mcp_error(-32600, "Invalid request"))
    assert not is_transport_stall(mcp_error(-32601, "Method not found"))
    assert not is_transport_stall(mcp_error(-32603, "Internal error"))
    assert not is_transport_stall(RuntimeError("server said no"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_echo_server() -> FastMCP:
    server = FastMCP(name="FlakyUpstream")

    @server.tool(name="echo", description="Echo back")
    def echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return f"echo:{message}"

    return server


async def start_server(port: int) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    """Start a fresh FastMCP streamable-http server and wait until it
    accepts connections (any HTTP status counts — readiness, not
    semantics)."""
    app = make_echo_server().streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    task = asyncio.create_task(server.serve())
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                await client.get(f"http://127.0.0.1:{port}/mcp")
                return server, task
            except (httpx.TransportError, OSError):
                # Catch the full transport/OS surface, not just
                # ConnectError: a CPU-starved loopback connect under
                # make test-all raises ``httpx.ConnectTimeout``, which
                # is a *sibling* of ConnectError (both under
                # TransportError), not a subclass — the old narrow
                # except let it escape and failed the test.
                await asyncio.sleep(0.05)
    raise AssertionError(f"upstream server on :{port} never came up")


async def stop_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_route_call_recovers_after_real_server_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = free_port()
    upstream = make_upstream_definition(
        id=UPSTREAM_ID,
        url=f"http://127.0.0.1:{port}/mcp",
        auth=UpstreamAuthConfig(mode=AuthMode.per_user_oauth),
    )
    client_manager = UpstreamClientManager([upstream])
    registry = ToolRegistry([upstream], client_manager)
    registry._tools = [
        make_discovered_tool(
            upstream_id=UPSTREAM_ID,
            original_name="echo",
            annotations=ToolAnnotations(idempotentHint=True),
        ),
    ]
    router = ToolRouter(
        registry,
        client_manager,
        FileAuditRepository(tmp_path / "audit.jsonl"),
        [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
        connection_store=AsyncMock(spec=ConnectionStore),
        server_url="http://localhost:8000",
    )

    # Stand-in for the OAuth token dance only: a "reconnect from stored
    # tokens" that re-opens the per-user session over the real adapter.
    reconnects: list[str] = []

    async def fake_reconnect(**kwargs: Any) -> None:
        reconnects.append(kwargs["effective_user"])
        await client_manager.connect_upstream_for_user(
            upstream, kwargs["effective_user"], bearer_token="t",
        )
        return None

    monkeypatch.setattr(
        ucs_module, "reconnect_with_stored_tokens", fake_reconnect
    )

    server, server_task = await start_server(port)
    try:
        # A real per-user session over the real HTTP adapter.
        await client_manager.connect_upstream_for_user(
            upstream, USER, bearer_token="t",
        )
        first = await router.route_call(
            org_id=DEFAULT_ORG_ID,
            prefixed_name=f"{UPSTREAM_ID}__echo",
            arguments={"message": "before"},
            user_id=USER,
            session_id=None,
        )
        assert not first.isError
        assert "echo:before" in first.content[0].text  # type: ignore[union-attr]

        # Kill the upstream mid-session — the prod idle-death, compressed.
        await stop_server(server, server_task)
        # Let the client's transport task observe the drop and unwind
        # (this is what closes the cached session's memory streams).
        await asyncio.sleep(0.5)
        assert client_manager.has_user_session(UPSTREAM_ID, USER), (
            "precondition: the dead session is still cached — the exact "
            "state the prod incident got stuck in"
        )

        server, server_task = await start_server(port)

        # One route_call must recover: stall on the dead session →
        # evict → reconnect → retry → real result.
        second = await router.route_call(
            org_id=DEFAULT_ORG_ID,
            prefixed_name=f"{UPSTREAM_ID}__echo",
            arguments={"message": "after"},
            user_id=USER,
            session_id=None,
        )
        assert not second.isError, (
            "the in-call retry must land on a fresh transport, got: "
            f"{second.content[0].text}"  # type: ignore[union-attr]
        )
        assert "echo:after" in second.content[0].text  # type: ignore[union-attr]
        assert reconnects == [USER]
    finally:
        await client_manager.disconnect_user_session(UPSTREAM_ID, USER)
        await stop_server(server, server_task)
