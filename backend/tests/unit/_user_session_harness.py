"""Harness for per-user session races, driven by a REAL streamable-HTTP
MCP server on loopback, a REAL file-backed token store and the REAL
reconnect path.

The one control knob is the test server's per-connection startup hook
(``ConnectionGate``): it can hold a chosen connection open, which parks a
connect in flight with no sleeps. The server numbers connections in
arrival order; one stored-token reconnect makes two, the token probe
first, then the real MCP connection.

NOTE: no ``from __future__ import annotations`` — FastMCP tool
registration calls ``issubclass()`` on annotations.
"""
import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from mcp.client.session import ClientSession
from mcp.server.fastmcp import Context, FastMCP

from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as StoredToken,
)
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.upstream_connection_service import (
    acquire_upstream_session,
)
from tests.unit._loopback_mcp import free_port, wait_for_health
from tests.unit.factories import make_oauth_upstream

ALICE = "alice@co.com"
BOB = "bob@co.com"
UPSTREAM_ID = "drop"
GATEWAY_URL = "http://localhost:8000"


class ConnectionGate:
    """Numbers the connections the test server accepts, and holds the
    ones whose number is in ``hold`` open until ``release`` is set."""

    def __init__(self, hold: set[int] | None = None) -> None:
        self.opened = 0
        self.hold: set[int] = hold if hold is not None else set()
        self.release = asyncio.Event()

def make_upstream_server(gate: ConnectionGate) -> FastMCP:
    """A remote MCP server shaped like ``drop``: an ``echo`` tool, plus a
    ``whoami`` tool that reports the bearer token the connection was
    opened with, so a test can see WHICH sign-in a session carries."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP):  # type: ignore[no-untyped-def]
        gate.opened += 1
        if gate.opened in gate.hold:
            await gate.release.wait()
        yield {}

    server = FastMCP(name="DropLike", lifespan=lifespan)

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

async def start_upstream(
    gate: ConnectionGate,
) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    app = make_upstream_server(gate).streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    task = asyncio.create_task(server.serve())
    await wait_until_serving(port)
    return server, task, url

async def wait_until_serving(port: int) -> None:
    """Readiness on a path the MCP app does not serve. A request to the
    MCP path itself opens a server session, which would move the
    connection count the tests assert on."""
    await wait_for_health(
        f"http://127.0.0.1:{port}/", label="drop-like upstream",
    )

async def stop_upstream(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)

def make_upstream(url: str) -> UpstreamDefinition:
    return make_oauth_upstream(
        id=UPSTREAM_ID, display_name="Drop", mode=AuthMode.per_user_oauth,
        url=url,
    )

def make_token(access_token: str) -> StoredToken:
    return StoredToken(
        access_token=access_token,
        refresh_token=f"refresh-{access_token}",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=[],
    )

async def make_store(
    tmp_path: Path, tokens: dict[str, str],
) -> FileConnectionStore:
    """A real file-backed store holding a live sign-in per user."""
    store = FileConnectionStore(tmp_path)
    for user, access_token in tokens.items():
        await store.put_user_token(
            DEFAULT_ORG_ID, user, UPSTREAM_ID, make_token(access_token),
        )
    return store

async def acquire(
    mgr: UpstreamClientManager,
    upstream: UpstreamDefinition,
    store: FileConnectionStore,
    user: str = ALICE,
) -> ClientSession:
    return await acquire_upstream_session(
        org_id=DEFAULT_ORG_ID,
        upstream=upstream,
        effective_user=user,
        connection_store=store,
        client_manager=mgr,
        server_url=GATEWAY_URL,
    )

async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    """Poll until ``predicate`` holds; fail loudly instead of hanging."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)
