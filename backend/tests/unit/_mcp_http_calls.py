"""Call an MCP endpoint's tool over streamable HTTP, in process, the way
an MCP client does: ``initialize``, the ``initialized`` notification,
then ``tools/call``.

For tests that need the session manager's real request handling: the
tool's handler then runs in the session manager's task group, not in the
HTTP request's task, as in production. The in-memory transport
(``create_connected_server_and_client_session``) has no session manager.
"""
from __future__ import annotations

from typing import Any

import httpx
from starlette.types import ASGIApp

from tests.unit._admin_mcp_harness import MCP_ACCEPT, make_initialize_body


async def post_mcp_message(
    client: httpx.AsyncClient, body: dict[str, Any], session: str | None,
) -> httpx.Response:
    headers = {"Accept": MCP_ACCEPT, "Content-Type": "application/json"}
    if session is not None:
        headers["mcp-session-id"] = session
    return await client.post("/", json=body, headers=headers)


async def call_tool_over_http(
    app: ASGIApp, tool: str, arguments: dict[str, Any] | None = None,
) -> httpx.Response:
    """Open a session on ``app``, an MCP endpoint served at ``/`` whose
    session manager runs, and call ``tool``. Returns the answer to the
    call."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        timeout=60,
    ) as client:
        init = await post_mcp_message(client, make_initialize_body(), None)
        session = init.headers["mcp-session-id"]
        await post_mcp_message(
            client,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session,
        )
        return await post_mcp_message(client, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }, session)
