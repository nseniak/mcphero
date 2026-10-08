"""Every MCP request runs with its own caller and session id.

The MCP SDK handles every request of a session inside the task it
started at ``initialize``, so per-request context variables held what
they held then. ``current_session_id`` was None (``initialize`` carries
no session id): tool-call audit rows, denied-call rows and handler log
lines had no session id. ``current_user_id`` was "anonymous" for every
bearer client (only the dashboard cookie sets it), so log lines and
Sentry events named "anonymous".
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import Server, request_ctx
from mcp.shared.context import RequestContext
from mcp.types import EmptyResult, ServerResult
from pydantic import BaseModel
from starlette.requests import Request
from structlog.contextvars import get_contextvars

from mcpolis.domain.model.email_allowlist import EmailAllowlist
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.app import _build_superadmin_app_with_oauth
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_session_id,
    current_user_id,
)
from mcpolis.entrypoints.controllers.superadmin_controller import (
    create_superadmin_mcp_server,
)
from mcpolis.entrypoints.middleware.mcp_request_identity import (
    RequestIdentityHandler,
    bind_request_identity,
)
from tests.unit._loopback_mcp import await_tools_ready
from tests.unit.factories import make_runtime_manager
from tests.unit.test_cloud_mcp_session_owner import (
    ACME,
    ALICE,
    BASE_URL,
    ROOT,
    make_client,
    make_cloud_admin_mcp,
    make_cloud_gateway,
    make_cloud_settings,
    make_org_repo,
    make_org_service,
    make_provider,
)
from tests.unit.test_gateway_service_tokens import _start_stack, _stop_stack
from tests.unit.test_gateway_session_owner import (
    ADMIN,
    MEMBER,
    make_gateway_url,
    open_session,
    rpc_result,
    send_request,
)

WHOAMI = {"name": "probe_whoami", "arguments": {}}


def add_whoami_tool(server: FastMCP) -> None:
    """A tool reporting what every tool of ``server`` sees per request."""

    @server.tool(name="probe_whoami")
    async def probe_whoami() -> str:  # pyright: ignore[reportUnusedFunction]
        return json.dumps({
            "user_id": current_user_id.get(),
            "session_id": current_session_id.get(),
        })


def read_audit_rows(tmp_path: Path) -> list[dict[str, object]]:
    audit_path = tmp_path / "data" / "audit.jsonl"
    return [
        json.loads(line)
        for line in audit_path.read_text().strip().splitlines()
    ]


@pytest.mark.asyncio
async def test_tool_call_audit_rows_carry_their_session_id(
    tmp_path: Path,
) -> None:
    """An allowed call and a denied call, each in its own session: each
    audit row names the session it came from."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        provider = app.state.mcp_gateway_oauth_provider
        admin = await provider.mint_test_token(ADMIN)
        member = await provider.mint_test_token(MEMBER)
        await await_tools_ready(url, admin, "fake__greet")
        greet = {"name": "fake__greet", "arguments": {"name": "World"}}
        async with httpx.AsyncClient(timeout=30.0) as client:
            admin_session = await open_session(client, url, admin)
            member_session = await open_session(client, url, member)
            rpc_result(await send_request(
                client, url, admin, admin_session, "tools/call", greet,
            ))
            rpc_result(await send_request(
                client, url, member, member_session, "tools/call", greet,
            ))

        calls = [
            row for row in read_audit_rows(tmp_path)
            if row.get("tool") == "fake__greet"
        ]
        session_of = {
            str(row["policy_decision"]): row.get("session_id") for row in calls
        }
        assert session_of == {
            "allowed": admin_session, "denied": member_session,
        }
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_admin_tools_see_the_signed_in_admin_and_the_session(
    tmp_path: Path,
) -> None:
    parent, admin_mcp, provider, _ = make_cloud_admin_mcp(tmp_path)
    add_whoami_tool(admin_mcp)
    alice = await provider.mint_test_token(ALICE)
    url = f"{BASE_URL}/admin-mcp/{ACME}/"
    async with admin_mcp.session_manager.run(), make_client(parent) as client:
        session = await open_session(client, url, alice)

        seen = json.loads(rpc_result(await send_request(
            client, url, alice, session, "tools/call", WHOAMI,
        ))["content"][0]["text"])
        assert seen == {"user_id": ALICE, "session_id": session}


# ─────────────── the binding itself ───────────────


class Seen(BaseModel):
    user_id: str
    session_id: str | None
    logged_session_id: object


def make_http_request(
    *, session_id: str | None, user_id: str | None,
) -> Request:
    headers = (
        [] if session_id is None
        else [(b"mcp-session-id", session_id.encode())]
    )
    scope: dict[str, Any] = {
        "type": "http", "method": "POST", "path": "/", "headers": headers,
        "query_string": b"",
    }
    if user_id is not None:
        scope["user"] = AuthenticatedUser(
            AccessToken(token="t", client_id=user_id, scopes=[], expires_at=None),
        )
    return Request(scope)


def make_recording_handler(seen: list[Seen]) -> RequestIdentityHandler:
    async def handler(_request: object) -> ServerResult:
        seen.append(Seen(
            user_id=current_user_id.get(),
            session_id=current_session_id.get(),
            logged_session_id=get_contextvars().get("session_id"),
        ))
        return ServerResult(EmptyResult())

    return RequestIdentityHandler(handler)


async def handle_over(
    handler: RequestIdentityHandler, http_request: Request | None,
) -> None:
    """Run ``handler`` the way the SDK does: with ``request_ctx`` set."""
    reset = request_ctx.set(RequestContext[Any, Any, Any](
        request_id=1, meta=None, session=None, lifespan_context=None,
        request=http_request,
    ))
    try:
        await handler(None)
    finally:
        request_ctx.reset(reset)


@pytest.mark.asyncio
async def test_a_request_runs_with_its_own_caller_and_session_id() -> None:
    seen: list[Seen] = []
    handler = make_recording_handler(seen)

    await handle_over(
        handler, make_http_request(session_id="s-1", user_id="alice@acme.test"),
    )

    assert seen == [Seen(
        user_id="alice@acme.test", session_id="s-1", logged_session_id="s-1",
    )]
    # Back to what the session task held once the request is done.
    assert (current_user_id.get(), current_session_id.get()) == (
        "anonymous", None,
    )


@pytest.mark.asyncio
async def test_a_request_without_a_signed_in_caller_keeps_the_user() -> None:
    seen: list[Seen] = []
    await handle_over(
        make_recording_handler(seen),
        make_http_request(session_id="s-1", user_id=None),
    )
    assert seen[0].user_id == "anonymous"
    assert seen[0].session_id == "s-1"


@pytest.mark.asyncio
async def test_a_request_not_over_http_changes_nothing() -> None:
    seen: list[Seen] = []
    await handle_over(make_recording_handler(seen), None)
    assert seen == [Seen(
        user_id="anonymous", session_id=None, logged_session_id=None,
    )]


@pytest.mark.asyncio
async def test_a_request_marks_its_session_busy_while_it_runs() -> None:
    """A tool call keeps running after its client hangs up (its HTTP
    request is over), so the handler itself marks its session busy."""
    events: list[str] = []

    @contextmanager
    def busy(session_id: str | None) -> Iterator[None]:
        events.append(f"busy {session_id}")
        yield
        events.append(f"free {session_id}")

    async def handler(_request: object) -> ServerResult:
        events.append("handled")
        return ServerResult(EmptyResult())

    await handle_over(
        RequestIdentityHandler(handler, busy),
        make_http_request(session_id="s-1", user_id="alice@acme.test"),
    )

    assert events == ["busy s-1", "handled", "free s-1"]


def test_binding_twice_wraps_once() -> None:
    server: Server[Any, Any] = Server("binding-test")

    @server.list_tools()
    async def list_tools() -> list[Any]:  # pyright: ignore[reportUnusedFunction]
        return []

    bind_request_identity(server)
    bound = dict(server.request_handlers)
    bind_request_identity(server)
    assert server.request_handlers == bound


@pytest.mark.asyncio
async def test_every_mcp_endpoint_binds_every_request_handler(
    tmp_path: Path,
) -> None:
    """Gateway, admin MCP and superadmin MCP, as the app builds them."""
    _, gateway_sessions, _ = make_cloud_gateway(tmp_path)
    _, admin_mcp, _, _ = make_cloud_admin_mcp(tmp_path)
    runtime_manager = make_runtime_manager(PolicyEngine(SettingsConfig()))
    org_repo = make_org_repo()
    superadmin_mcp = create_superadmin_mcp_server(
        org_repo=org_repo,  # type: ignore[arg-type]
        runtime_manager=runtime_manager,
        org_service=make_org_service(org_repo),
    )
    _build_superadmin_app_with_oauth(
        superadmin_mcp, make_provider(runtime_manager), make_cloud_settings(),
        EmailAllowlist([ROOT]),
    )

    for server in (
        gateway_sessions.app,
        admin_mcp.session_manager.app,
        superadmin_mcp.session_manager.app,
    ):
        unbound = [
            request_type.__name__
            for request_type, handler in server.request_handlers.items()
            if not isinstance(handler, RequestIdentityHandler)
        ]
        assert unbound == [], (server.name, unbound)
