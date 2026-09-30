"""A request in flight when its connection ends is answered at once.

The MCP SDK answers the requests still waiting when its read loop ends,
in a ``finally``. But a session torn down by cancellation, which is how
a failing transport ends it and also how a close ends it, cancels that
``finally`` at its first ``await``. The waiting request then hung until
the tool router's liveness probe, 30 s later.

Production shape (e2e 18c, 2026-09-30): an open connection's token
refresh got a 503, the SDK sent the tool call unsigned, the upstream
answered 401, and our silent sign-in refused to open a browser. That
refusal, raised inside the connection's transport, killed the
connection with the tool call still waiting on it.

REAL loopback MCP servers over streamable HTTP, REAL client manager.

NOTE: no ``from __future__ import annotations`` (see the race harness).
"""
import asyncio
import gc
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.connection_task_base import (
    in_flight_connection_loss,
    is_in_flight_connection_loss,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.tool_registry import is_transport_stall
from mcpolis.domain.services.tool_router import (  # pyright: ignore[reportPrivateUsage]
    _is_post_delivery_stall,
    dispatch_with_liveness,
)
from mcpolis.domain.services.upstream_connection_service import (
    SilentReconnectAuthRequired,
)
from tests.unit._loopback_mcp import free_port
from tests.unit._user_session_harness import (
    ALICE,
    UPSTREAM_ID,
    ConnectionGate,
    make_upstream,
    start_upstream,
    stop_upstream,
    wait_until_serving,
)

# Well under the router's 30 s liveness probe: answered "at once".
ANSWERED_WITHIN = 5.0


class RefuseSignIn(httpx.Auth):
    """Lets the handshake through, then fails the requests of one method
    inside the auth flow, the way our silent sign-in fails a request when
    a refresh was rejected and the upstream asks for a browser sign-in."""

    def __init__(self, method: str) -> None:
        self._marker = f'"method":"{method}"'.encode()

    async def async_auth_flow(
        self, request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self._marker in request.content.replace(b" ", b""):
            raise SilentReconnectAuthRequired(
                "unexpected callback during silent reconnect",
            )
        yield request


class HeldCall:
    """A tool that answers only once ``release`` is set; ``started`` is
    set when the upstream begins running it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()


async def start_slow_upstream(
    held: HeldCall,
) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    server = FastMCP(name="Slow")

    @server.tool(name="wait", description="Answers once released")
    async def wait() -> str:  # pyright: ignore[reportUnusedFunction]
        held.started.set()
        await held.release.wait()
        return "released"

    port = free_port()
    uv = uvicorn.Server(uvicorn.Config(
        server.streamable_http_app(), host="127.0.0.1", port=port,
        log_level="warning", ws="none",
    ))
    task = asyncio.create_task(uv.serve())
    await wait_until_serving(port)
    return uv, task, f"http://127.0.0.1:{port}/mcp"


@pytest.mark.asyncio
async def test_a_call_in_flight_fails_at_once_when_its_connection_dies() -> None:
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    mgr = UpstreamClientManager([upstream])
    try:
        session = await mgr.ensure_user_session(
            upstream, ALICE, auth=RefuseSignIn("tools/call"),
        )

        with pytest.raises(McpError) as failed:
            await asyncio.wait_for(
                session.call_tool("echo", {"message": "hi"}),
                timeout=ANSWERED_WITHIN,
            )

        assert is_in_flight_connection_loss(failed.value), failed.value
        assert mgr.find_user_session(UPSTREAM_ID, ALICE) is None, (
            "the dead session is still handed out to the next call"
        )
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)


@pytest.mark.asyncio
async def test_a_call_in_flight_fails_at_once_when_its_session_is_closed() -> None:
    """A Disconnect (or a replaced session) closes the connection on
    purpose while a tool is still running upstream."""
    held = HeldCall()
    server, server_task, url = await start_slow_upstream(held)
    upstream = make_upstream(url)
    mgr = UpstreamClientManager([upstream])
    try:
        session = await mgr.ensure_user_session(upstream, ALICE)
        call = asyncio.create_task(session.call_tool("wait", {}))
        await asyncio.wait_for(held.started.wait(), timeout=ANSWERED_WITHIN)

        await mgr.disconnect_user_session(UPSTREAM_ID, ALICE)

        with pytest.raises(McpError) as failed:
            await asyncio.wait_for(call, timeout=ANSWERED_WITHIN)
        assert is_in_flight_connection_loss(failed.value), failed.value
    finally:
        held.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)



@pytest.mark.asyncio
async def test_a_connection_dying_during_the_liveness_ping_leaves_no_unread_error() -> None:
    """The liveness probe's ping can be what kills the connection (its
    token refresh fails, as in 18c). The ping and the long call are then
    answered together. The dispatch surfaces a possibly-delivered stall,
    and reads the call's own error: an unread one makes asyncio log "Task
    exception was never retrieved" at ERROR, which reaches Sentry."""
    held = HeldCall()
    server, server_task, url = await start_slow_upstream(held)
    upstream = make_upstream(url)
    mgr = UpstreamClientManager([upstream])
    loop = asyncio.get_running_loop()
    unread: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unread.append(context))
    try:
        session = await mgr.ensure_user_session(
            upstream, ALICE, auth=RefuseSignIn("ping"),
        )

        # Keep only the verdict: holding the exception would keep the
        # dispatch's frames, and with them the call's task, alive past the
        # check below.
        verdict: tuple[bool, bool] | None = None
        try:
            await asyncio.wait_for(dispatch_with_liveness(
                session, lambda: session.call_tool("wait", {}),
                op_label="wait", org_id=DEFAULT_ORG_ID, upstream_id=UPSTREAM_ID,
                probe_interval=0.3, ping_timeout=2.0,
            ), timeout=ANSWERED_WITHIN)
        except Exception as exc:
            verdict = (is_transport_stall(exc), _is_post_delivery_stall(exc))

        assert verdict == (True, True), verdict
        gc.collect()
        await asyncio.sleep(0)
        assert unread == [], [c.get("message") for c in unread]
    finally:
        loop.set_exception_handler(previous)
        held.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


class UnansweredPing:
    """A session whose ping is never answered."""

    async def send_ping(self) -> None:
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_dispatch_cancelled_after_its_op_failed_leaves_no_unread_error() -> None:
    """The op fails while the liveness ping is out, and the dispatch is
    then cancelled from outside (the caller hung up) before the ping
    returns. The op's error must still be read."""
    loop = asyncio.get_running_loop()
    unread: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unread.append(context))
    op_failed = asyncio.Event()

    async def op() -> None:
        await asyncio.sleep(0.05)  # past the probe interval: a ping is out
        op_failed.set()
        raise McpError(in_flight_connection_loss())

    try:
        dispatch = asyncio.create_task(dispatch_with_liveness(
            UnansweredPing(), op, op_label="op", org_id=DEFAULT_ORG_ID,
            upstream_id=UPSTREAM_ID, probe_interval=0.01, ping_timeout=5.0,
        ))
        await asyncio.wait_for(op_failed.wait(), timeout=ANSWERED_WITHIN)
        await asyncio.sleep(0)
        dispatch.cancel()
        cancelled = False
        try:
            await dispatch
        except asyncio.CancelledError:
            cancelled = True
        del dispatch

        assert cancelled
        gc.collect()
        await asyncio.sleep(0)
        assert unread == [], [c.get("message") for c in unread]
    finally:
        loop.set_exception_handler(previous)


class PingBreaksAfter:
    """A session whose ping fails with a dead transport once ``op_done``
    is set: the connection died just after the op got its answer."""

    def __init__(self, op_done: asyncio.Event) -> None:
        self._op_done = op_done

    async def send_ping(self) -> None:
        await self._op_done.wait()
        await asyncio.sleep(0)  # the op's task finishes first
        raise anyio.ClosedResourceError()


@pytest.mark.asyncio
async def test_an_op_answered_just_before_its_connection_died_keeps_its_result() -> None:
    """A tool that ran and answered keeps its answer, even if the ping
    finds the connection dead right after. Reporting a stall instead
    would lose the result, and a user retrying by hand would run a
    non-repeatable tool twice."""
    op_done = asyncio.Event()

    async def op() -> str:
        await asyncio.sleep(0.05)  # past the probe interval: a ping is out
        op_done.set()
        return "the answer"

    result = await asyncio.wait_for(dispatch_with_liveness(
        PingBreaksAfter(op_done), op, op_label="op", org_id=DEFAULT_ORG_ID,
        upstream_id=UPSTREAM_ID, probe_interval=0.01, ping_timeout=5.0,
    ), timeout=ANSWERED_WITHIN)

    assert result == "the answer"
