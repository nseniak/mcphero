"""End to end: an MCP added WITH a service-account token connects to a
real upstream that REQUIRES that token, and lists / calls its tools.

``test_service_account_token.py`` checks what is saved and, on the wire,
which header a refusing server receives. These drive real MCP servers:
an HTTP FastMCP behind a middleware that answers 401 unless the exact
bearer arrives, and a stdio FastMCP (local subprocess) that reports the
MCP_AUTH_TOKEN it was started with.

NOTE: no ``from __future__ import annotations``: FastMCP tool
registration inspects annotations.
"""
import asyncio
import contextlib
import io
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import structlog
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import TextContent

from mcpolis.adapters.observability.structlog_setup import configure_structlog
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit._loopback_mcp import free_port, wait_for_health
from tests.unit.test_admin_mcp import (
    _build_admin_server,  # pyright: ignore[reportPrivateUsage]
    _call,  # pyright: ignore[reportPrivateUsage]
    _config_users_only_admin,  # pyright: ignore[reportPrivateUsage]
)
from tests.unit.test_dashboard_api import make_test_client
from tests.unit.test_service_account_token import make_file_store_over

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

ENV_ECHO_MCP = '''
import os
from mcp.server.fastmcp import FastMCP

server = FastMCP(name="EnvEcho")


@server.tool(name="auth_token", description="The MCP_AUTH_TOKEN I got")
def auth_token() -> str:
    return os.environ.get("MCP_AUTH_TOKEN", "unset")


if __name__ == "__main__":
    server.run()
'''


def make_bearer_checking_app(app: App, expected_token: str) -> App:
    """401 unless the request carries exactly one Authorization header,
    ``Bearer <expected_token>``. Lifespan scopes pass through."""
    expected = [f"Bearer {expected_token}".encode()]

    async def checked(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            got = [v for k, v in scope["headers"] if k == b"authorization"]
            if got != expected:
                await send({
                    "type": "http.response.start", "status": 401,
                    "headers": [(b"content-length", b"0")],
                })
                await send({"type": "http.response.body", "body": b""})
                return
        await app(scope, receive, send)

    return checked


def make_token_protected_server() -> FastMCP:
    server = FastMCP(name="TokenProtected")

    @server.tool(name="echo", description="Echo back the message")
    def echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return f"echo:{message}"

    @server.tool(name="whoami", description="The bearer this session uses")
    def whoami(ctx: Context) -> str:  # type: ignore[type-arg]  # pyright: ignore[reportUnusedFunction, reportMissingTypeArgument, reportUnknownParameterType]
        request = ctx.request_context.request
        if request is None:
            return "no-request"
        return str(request.headers.get("authorization", "none"))  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

    return server


@asynccontextmanager
async def run_token_protected_upstream(token: str) -> AsyncIterator[str]:
    """Serve the protected MCP on loopback; yield its /mcp URL."""
    port = free_port()
    app = make_bearer_checking_app(
        make_token_protected_server().streamable_http_app(), token,
    )
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    task = asyncio.create_task(server.serve())
    try:
        # Any path answers (401 is a response); only reachability matters.
        await wait_for_health(f"http://127.0.0.1:{port}/", label="protected")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def call_text(
    manager: UpstreamClientManager, upstream: UpstreamDefinition, tool: str,
) -> str:
    """Start the upstream the way the dashboard's Start does
    (``connect_upstream`` on what the store returns) and call *tool*."""
    session = await manager.connect_upstream(upstream)
    result = await session.call_tool(tool, {})
    first = result.content[0]
    assert isinstance(first, TextContent), result
    return first.text


@contextlib.contextmanager
def capture_production_logs() -> Iterator[io.StringIO]:
    """The app's own logging setup (JSON, INFO) writing into a buffer;
    restores the previous logging state afterwards."""
    root = logging.getLogger()
    old_handlers, old_level = list(root.handlers), root.level
    old_structlog = structlog.get_config()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            configure_structlog(json_logs=True, log_level="INFO")
        yield buf
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in old_handlers:
            root.addHandler(handler)
        root.setLevel(old_level)
        structlog.configure(**old_structlog)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [
    "tok-e2e",
    # Saved as a Variable's value, which substitution never expands.
    "tok-${HOME}-e2e",
])
async def test_http_token_from_admin_mcp_authenticates_against_a_real_mcp(
    token: str, tmp_path: Path,
) -> None:
    async with run_token_protected_upstream(token) as url:
        server, _ = await _build_admin_server(
            tmp_path, config=_config_users_only_admin(), plan=PlanName.team,
        )
        added = await _call(server, "add_upstream", {
            "mcp_id": "remote", "display_name": "Remote",
            "transport": "streamable_http", "url": url,
            "auth_token": token,
        })
        assert "added" in added, added

        # The reporter's own path: the Admin MCP starts it and lists tools.
        started = await _call(server, "start_upstream", {"mcp_id": "remote"})
        assert "2 tools available" in started, started
        await _call(server, "disconnect_upstream", {"mcp_id": "remote"})

        # The dashboard's Start path, after a restart: a fresh store.
        upstream = await make_file_store_over(tmp_path).get(
            DEFAULT_ORG_ID, "remote",
        )
        assert upstream is not None
        manager = UpstreamClientManager(
            upstreams=[upstream],
            template_var_repo=FileTemplateVarRepository(tmp_path / "data"),
        )
        try:
            assert await call_text(manager, upstream, "whoami") == (
                f"Bearer {token}"
            )
        finally:
            await manager.stop_all()


@pytest.mark.asyncio
async def test_token_with_trailing_newline_authenticates_and_stays_out_of_logs(
    tmp_path: Path,
) -> None:
    """A token read from a file ends in a newline. Kept as is, the HTTP
    client refuses the header on every connect and logs the refusal
    with the whole token in the message."""
    secret = "tok-secret-5a4b3c2d1e"
    async with run_token_protected_upstream(secret) as url:
        server, _ = await _build_admin_server(
            tmp_path, config=_config_users_only_admin(), plan=PlanName.team,
        )
        added = await _call(server, "add_upstream", {
            "mcp_id": "remote", "display_name": "Remote",
            "transport": "streamable_http", "url": url,
            "auth_token": secret + "\n",
        })
        assert "added" in added, added
        with capture_production_logs() as logs:
            started = await _call(
                server, "start_upstream", {"mcp_id": "remote"},
            )
        await _call(server, "disconnect_upstream", {"mcp_id": "remote"})

    assert "2 tools available" in started, started
    leaked = [line for line in logs.getvalue().splitlines() if secret in line]
    assert not leaked, [line[:160] for line in leaked]


@pytest.mark.asyncio
async def test_move_to_variables_shape_authenticates_against_a_real_mcp(
    tmp_path: Path,
) -> None:
    """The exact body the dashboard sends after "Move to Variables":
    the header as ``Bearer ${API_KEY}``, the raw token, and the Variable."""
    async with run_token_protected_upstream("live-key") as url:
        client = make_test_client(tmp_path)
        resp = client.post("/api/admin/upstreams", json={
            "id": "remote", "display_name": "Remote", "url": url,
            "headers": {"Authorization": "Bearer ${API_KEY}"},
            "auth_mode": "service_account",
            "auth_token": "live-key",
            "template_vars": {"API_KEY": {"value": "live-key", "is_secret": True}},
        })
        assert resp.status_code == 201, resp.text

        upstream = await make_file_store_over(tmp_path).get(
            DEFAULT_ORG_ID, "remote",
        )
        assert upstream is not None
        manager = UpstreamClientManager(
            upstreams=[upstream],
            template_var_repo=FileTemplateVarRepository(tmp_path / "data"),
        )
        try:
            assert await call_text(manager, upstream, "whoami") == (
                "Bearer live-key"
            )
        finally:
            await manager.stop_all()


@pytest.mark.asyncio
async def test_stdio_token_from_admin_mcp_reaches_the_process_env(
    tmp_path: Path,
) -> None:
    script = tmp_path / "env_echo_mcp.py"
    script.write_text(ENV_ECHO_MCP)
    assert "," not in str(script)  # add_upstream splits args on commas
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(), plan=PlanName.team,
    )
    added = await _call(server, "add_upstream", {
        "mcp_id": "local", "display_name": "Local", "transport": "stdio",
        "command": sys.executable, "args": str(script),
        "auth_token": "tok-stdio-e2e",
    })
    assert "added" in added, added

    upstream = await make_file_store_over(tmp_path).get(DEFAULT_ORG_ID, "local")
    assert upstream is not None
    manager = UpstreamClientManager(
        upstreams=[upstream],
        template_var_repo=FileTemplateVarRepository(tmp_path / "data"),
    )
    try:
        assert await call_text(manager, upstream, "auth_token") == (
            "tok-stdio-e2e"
        )
    finally:
        await manager.stop_all()
