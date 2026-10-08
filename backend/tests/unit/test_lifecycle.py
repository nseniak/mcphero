"""Tests for graceful drain lifecycle (Phase 4, Step 4)."""
from __future__ import annotations

import asyncio
import signal

import pytest

from starlette.types import Message, Receive, Scope, Send

from mcpolis.entrypoints.lifecycle import (
    DrainCoordinator,
    DrainMiddleware,
    SignalHandler,
    drain_then_exit,
)


@pytest.mark.asyncio
async def test_drain_completes_immediately_when_idle() -> None:
    """Drain with no active requests completes immediately."""
    dc = DrainCoordinator(drain_timeout=5.0)
    assert dc.is_draining is False
    await dc.drain()
    assert dc.is_draining is True


@pytest.mark.asyncio
async def test_drain_waits_for_active_requests() -> None:
    """Drain waits until all in-flight requests finish."""
    dc = DrainCoordinator(drain_timeout=5.0, quiet_seconds=0.05)
    dc.request_started()
    dc.request_started()
    assert dc.active_requests == 2

    # Start drain in background
    drain_done = False

    async def do_drain() -> None:
        nonlocal drain_done
        await dc.drain()
        drain_done = True

    task = asyncio.create_task(do_drain())
    await asyncio.sleep(0.05)
    assert not drain_done  # Still waiting

    dc.request_finished()
    await asyncio.sleep(0.05)
    assert not drain_done  # 1 request still active

    dc.request_finished()
    # Done once no request started for ``quiet_seconds``.
    await asyncio.wait_for(task, timeout=1.0)
    assert drain_done  # All done


@pytest.mark.asyncio
async def test_drain_times_out() -> None:
    """Drain times out if requests don't finish."""
    dc = DrainCoordinator(drain_timeout=0.1)
    dc.request_started()

    await dc.drain()  # Should return after timeout
    assert dc.is_draining is True
    assert dc.active_requests == 1  # Request still "active"


@pytest.mark.asyncio
async def test_request_tracking() -> None:
    """request_started / request_finished track counts correctly."""
    dc = DrainCoordinator()
    assert dc.active_requests == 0

    dc.request_started()
    assert dc.active_requests == 1

    dc.request_started()
    assert dc.active_requests == 2

    dc.request_finished()
    assert dc.active_requests == 1

    dc.request_finished()
    assert dc.active_requests == 0

    # Extra finish doesn't go negative
    dc.request_finished()
    assert dc.active_requests == 0


@pytest.mark.asyncio
async def test_healthz_endpoint_not_draining() -> None:
    """Verify DrainCoordinator starts not draining."""
    dc = DrainCoordinator()
    assert dc.is_draining is False


@pytest.mark.asyncio
async def test_healthz_endpoint_draining() -> None:
    """After drain, is_draining is True."""
    dc = DrainCoordinator(drain_timeout=0.1)
    await dc.drain()
    assert dc.is_draining is True


def make_exit_recorder() -> tuple[list[int], SignalHandler]:
    """A stand-in for uvicorn's exit handler that records each call."""
    exits: list[int] = []
    return exits, lambda sig, frame: exits.append(sig)


@pytest.mark.asyncio
async def test_sigterm_stops_the_server_only_after_in_flight_requests() -> None:
    """The server's exit handler runs once the drain is done, not before."""
    dc = DrainCoordinator(drain_timeout=5.0, quiet_seconds=0.05)
    exits, server_exit = make_exit_recorder()
    dc.request_started()

    task = asyncio.create_task(drain_then_exit(dc, server_exit))
    await asyncio.sleep(0.05)
    assert dc.is_draining is True
    assert exits == []

    dc.request_finished()
    await asyncio.wait_for(task, timeout=1.0)
    assert exits == [signal.SIGTERM]


@pytest.mark.asyncio
async def test_sigterm_stops_the_server_when_the_drain_times_out() -> None:
    """A request that never finishes does not keep the server up."""
    dc = DrainCoordinator(drain_timeout=0.05)
    exits, server_exit = make_exit_recorder()
    dc.request_started()

    await asyncio.wait_for(drain_then_exit(dc, server_exit), timeout=1.0)
    assert exits == [signal.SIGTERM]


@pytest.mark.asyncio
async def test_sigterm_without_a_server_handler_only_drains() -> None:
    dc = DrainCoordinator(drain_timeout=5.0)
    await drain_then_exit(dc, None)
    assert dc.is_draining is True


# ── DrainMiddleware: what counts as in flight ──


class _StreamingApp:
    """An ASGI app that streams its response in two chunks and waits on
    ``release`` between them, like an MCP tool call whose result comes
    at the end of a streamed response."""

    def __init__(self) -> None:
        self.headers_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        self.headers_sent.set()
        await self.release.wait()
        await send({"type": "http.response.body", "body": b"b"})


def make_scope(
    method: str = "POST",
    path: str = "/mcp/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Scope:
    return {
        "type": "http", "method": method, "path": path,
        "headers": headers or [],
    }


async def _no_receive() -> Message:
    return {"type": "http.disconnect"}


def make_sent_log() -> tuple[list[Message], Send]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    return sent, send


@pytest.mark.asyncio
async def test_a_streamed_response_counts_until_its_last_chunk() -> None:
    dc = DrainCoordinator()
    app = _StreamingApp()
    _sent, send = make_sent_log()
    call = asyncio.create_task(
        DrainMiddleware(app, dc)(make_scope(), _no_receive, send),
    )

    await app.headers_sent.wait()
    assert dc.active_requests == 1  # headers out, result still coming

    app.release.set()
    await call
    assert dc.active_requests == 0


@pytest.mark.asyncio
async def test_an_event_stream_is_not_counted() -> None:
    dc = DrainCoordinator()
    app = _StreamingApp()
    _sent, send = make_sent_log()
    scope = make_scope(
        method="GET", path="/api/events",
        headers=[(b"accept", b"text/event-stream")],
    )
    call = asyncio.create_task(DrainMiddleware(app, dc)(scope, _no_receive, send))

    await app.headers_sent.wait()
    assert dc.active_requests == 0

    app.release.set()
    await call


@pytest.mark.asyncio
async def test_draining_refuses_new_sessions_but_serves_open_ones() -> None:
    dc = DrainCoordinator()
    await dc.drain()
    app = _StreamingApp()
    app.release.set()
    middleware = DrainMiddleware(app, dc)

    refused, send_refused = make_sent_log()
    await middleware(make_scope(), _no_receive, send_refused)
    served, send_served = make_sent_log()
    await middleware(
        make_scope(headers=[(b"mcp-session-id", b"s1")]), _no_receive, send_served,
    )

    assert refused[0]["status"] == 503
    assert served[0]["status"] == 200


def header(message: Message, name: bytes) -> bytes | None:
    return next((v for k, v in message["headers"] if k == name), None)


@pytest.mark.asyncio
async def test_a_sign_in_page_refused_while_draining_gets_a_sentence() -> None:
    """A browser navigating to a sign-in step during a deploy reads a
    sentence, and when to try again."""
    dc = DrainCoordinator()
    await dc.drain()
    sent, send = make_sent_log()

    await DrainMiddleware(_StreamingApp(), dc)(
        make_scope(
            method="GET", path="/mcp/authorize",
            headers=[(b"accept", b"text/html")],
        ),
        _no_receive, send,
    )

    assert sent[0]["status"] == 503
    assert header(sent[0], b"content-type") == b"text/plain; charset=utf-8"
    assert header(sent[0], b"retry-after") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/events", "/api/admin/upstreams/u1/logs/stream"],
)
async def test_live_streams_are_served_while_draining(path: str) -> None:
    """A browser never retries an EventSource refused with a non-200."""
    dc = DrainCoordinator()
    await dc.drain()
    app = _StreamingApp()
    app.release.set()
    sent, send = make_sent_log()

    await DrainMiddleware(app, dc)(
        make_scope(
            method="GET", path=path,
            headers=[(b"accept", b"text/event-stream")],
        ),
        _no_receive, send,
    )

    assert sent[0]["status"] == 200
    assert dc.active_requests == 0


@pytest.mark.asyncio
async def test_the_drain_waits_for_a_quick_follow_up_request() -> None:
    """A client that gets its tool result often sends another request at
    once; the drain must not end in that gap."""
    dc = DrainCoordinator(drain_timeout=5.0, quiet_seconds=0.2)
    dc.request_started()
    drained = asyncio.create_task(dc.drain())
    await asyncio.sleep(0)  # the drain starts while the call runs

    dc.request_finished()  # the tool call ends...
    await asyncio.sleep(0.05)
    dc.request_started()  # ...and its follow-up arrives
    await asyncio.sleep(0.3)
    assert not drained.done()

    dc.request_finished()
    await asyncio.wait_for(drained, timeout=1.0)
