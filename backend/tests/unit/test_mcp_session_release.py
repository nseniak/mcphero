"""Ended MCP sessions leave nothing behind.

The MCP SDK keeps every DELETEd session in its table for the life of
the process, and it opens a session (with its running tasks) for any
request without a session id, even one it then refuses: a GET, a
non-``initialize`` POST. Measured on 2026-10-07 on the full gateway: 20
open + DELETE cycles left 20 sessions in the SDK's table and 20 in the
gateway's registry; 20 refused requests left 20 sessions and 60 running
tasks. Clients DELETE on every normal close, and any signed-in caller
can send refused requests.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from typing import Any

import httpx
import pytest
from mcp import types
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.types import Message, Scope

from mcpolis.entrypoints.app import _SessionRegistrationMiddleware
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.middleware.mcp_request_identity import (
    bind_request_identity,
)
from mcpolis.entrypoints.middleware.session_owner_guard import (
    SESSION_IDLE_LIMIT_SECONDS,
    SessionOwnerGuard,
)
from tests.unit.test_gateway_service_tokens import _start_stack, _stop_stack
from tests.unit.test_gateway_session_owner import (
    ADMIN,
    find_guard,
    make_gateway_url,
    make_headers,
    open_session,
)

# Tasks that may come and go on their own in the full app (timers,
# connection housekeeping). A leak is 3 tasks per refused request.
TASK_SLACK = 3


async def wait_for_task_count(at_most: int, timeout: float = 5.0) -> int:
    """Poll the event loop's task count until it is ``at_most`` or less."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        count = len(asyncio.all_tasks())
        if count <= at_most or asyncio.get_running_loop().time() > deadline:
            return count
        await asyncio.sleep(0.05)


async def wait_for_disconnect_rows(
    tmp_path: Path, expected: set[str], timeout: float = 5.0,
) -> set[str]:
    """Session ids of the ``client_disconnect`` audit rows, once all of
    ``expected`` are written (they are written in the background)."""
    audit_path = tmp_path / "data" / "audit.jsonl"
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        rows = [
            json.loads(line)
            for line in audit_path.read_text().strip().splitlines()
        ]
        found = {
            row["session_id"] for row in rows
            if row.get("action") == "client_disconnect"
        }
        if expected <= found or asyncio.get_running_loop().time() > deadline:
            return found
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_deleted_sessions_leave_nothing_behind(tmp_path: Path) -> None:
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        guard = find_guard(app, "/mcp")
        sdk_sessions = guard._session_manager._server_instances
        registration = guard._app
        assert isinstance(registration, _SessionRegistrationMiddleware)
        registry = registration._registry
        admin = await app.state.mcp_gateway_oauth_provider.mint_test_token(
            ADMIN,
        )
        deleted: set[str] = set()
        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(5):
                session = await open_session(client, url, admin)
                ended = await client.delete(
                    url, headers=make_headers(admin, session),
                )
                assert ended.status_code == 200, ended.text
                deleted.add(session)

        assert deleted.isdisjoint(sdk_sessions)
        assert not any(registry.has_session(sid) for sid in deleted)
        # Each end is in the audit log, at the time it happened.
        assert deleted <= await wait_for_disconnect_rows(tmp_path, deleted)
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_refused_requests_leave_no_session_running(
    tmp_path: Path,
) -> None:
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        guard = find_guard(app, "/mcp")
        sdk_sessions = guard._session_manager._server_instances
        admin = await app.state.mcp_gateway_oauth_provider.mint_test_token(
            ADMIN,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            await open_session(client, url, admin)  # warm the app up
            baseline = await wait_for_task_count(10_000)
            refused: list[str] = []
            for _ in range(5):
                stream = await client.get(
                    url,
                    headers={**make_headers(admin), "Accept": "text/event-stream"},
                )
                no_session = await client.post(
                    url,
                    headers=make_headers(admin),
                    json={
                        "jsonrpc": "2.0", "id": uuid.uuid4().hex,
                        "method": "tools/list",
                    },
                )
                for response in (stream, no_session):
                    assert response.status_code == 400, response.text
                    refused.append(response.headers["mcp-session-id"])

            assert set(refused).isdisjoint(sdk_sessions)
            remaining = await wait_for_task_count(baseline + TASK_SLACK)
            assert remaining <= baseline + TASK_SLACK, (baseline, remaining)
    finally:
        await _stop_stack(upstream, gateway, task)


# ─────────────── a tool call outliving its client ───────────────


class ManualClock(BaseModel):
    seconds: float = 1000.0

    def now(self) -> float:
        return self.seconds


class Reply(BaseModel):
    status: int
    session_id: str | None


INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}
SLOW_CALL: dict[str, Any] = {
    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
    "params": {"name": "slow", "arguments": {}},
}


def make_slow_tool_server(
    started: asyncio.Event, release: asyncio.Event, outcome: list[str],
) -> Server[Any, Any]:
    """One tool, ``slow``, running until ``release`` is set."""
    server: Server[Any, Any] = Server("slow-tool")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:  # pyright: ignore[reportUnusedFunction]
        return [types.Tool(name="slow", inputSchema={"type": "object"})]

    @server.call_tool(validate_input=False)
    async def call_tool(  # pyright: ignore[reportUnusedFunction]
        _name: str, _arguments: dict[str, Any],
    ) -> list[types.TextContent]:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        outcome.append("finished")
        return [types.TextContent(type="text", text="done")]

    return server


async def post_as(
    app: SessionOwnerGuard,
    user_id: str,
    body: dict[str, Any],
    session_id: str | None = None,
    hang_up: asyncio.Event | None = None,
) -> Reply:
    """One POST through ``app``; the client hangs up when ``hang_up``
    is set (never, without it)."""
    headers = [
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
        (b"mcp-protocol-version", b"2025-06-18"),
        (b"host", b"127.0.0.1"),
    ]
    if session_id is not None:
        headers.append((b"mcp-session-id", session_id.encode()))
    scope: Scope = {
        "type": "http", "method": "POST", "path": "/", "headers": headers,
        "query_string": b"", "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 1), "scheme": "http",
    }
    sent: list[Message] = []
    body_sent = False

    async def receive() -> Message:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {
                "type": "http.request", "body": json.dumps(body).encode(),
                "more_body": False,
            }
        await (hang_up or asyncio.Event()).wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    auth_reset = auth_context_var.set(AuthenticatedUser(
        AccessToken(token="t", client_id=user_id, scopes=[], expires_at=None),
    ))
    org_reset = current_org_id.set("acme-id")
    try:
        await app(scope, receive, send)
    finally:
        auth_context_var.reset(auth_reset)
        current_org_id.reset(org_reset)
    return Reply(
        status=sent[0]["status"],
        session_id=Headers(raw=sent[0]["headers"]).get("mcp-session-id"),
    )


@pytest.mark.asyncio
async def test_a_tool_call_whose_client_hung_up_is_not_cut_as_idle() -> None:
    """The HTTP request of a tool call ends when its client hangs up, but
    the call keeps running in the session. The session is not idle while
    it runs: an hour later it is still running, and it finishes."""
    started, release = asyncio.Event(), asyncio.Event()
    outcome: list[str] = []
    session_manager = StreamableHTTPSessionManager(
        app=make_slow_tool_server(started, release, outcome),
    )
    clock = ManualClock()
    # As ``_serve_sessions_per_caller`` wires it.
    guard = SessionOwnerGuard(
        session_manager.handle_request, session_manager, clock=clock.now,
    )
    bind_request_identity(session_manager.app, guard.busy)
    async with session_manager.run():
        opened = await post_as(guard, "alice@acme.test", INITIALIZE)
        session = opened.session_id
        assert opened.status == 200 and session is not None
        await post_as(
            guard, "alice@acme.test",
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, session,
        )
        hang_up = asyncio.Event()
        call = asyncio.create_task(
            post_as(guard, "alice@acme.test", SLOW_CALL, session, hang_up),
        )
        await started.wait()
        hang_up.set()
        await asyncio.wait_for(call, 5)

        clock.seconds += 3 * SESSION_IDLE_LIMIT_SECONDS
        await post_as(guard, "bob@acme.test", INITIALIZE)  # sweeps
        await asyncio.sleep(0.1)
        assert outcome == []
        assert session in session_manager._server_instances

        release.set()
        await asyncio.sleep(0.1)
        assert outcome == ["finished"]
