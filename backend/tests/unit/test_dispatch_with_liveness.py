"""``dispatch_with_liveness``: ping-on-timeout discrimination of a silent
transport stall from a live-but-slow server (R2).

The dispatch path runs MCP requests on a stdio ClientSession built with
NO read timeout, so a silent post-reattach stall (E2B #1128) would hang
unbounded. ``dispatch_with_liveness`` bounds it with a probe interval and
a liveness ping: a server that still answers pings is alive (the op runs
to completion, never torn down — the R2 anti-double-execute guarantee), a
server that stops answering is healed.

All tests inject tiny probe/ping intervals so they run in well under a
second; they pin the BEHAVIOUR, not the production timings.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import anyio
import mcp.types as mcp_types
import pytest
from mcp.shared.exceptions import McpError

from mcpolis.domain.services.tool_router import dispatch_with_liveness


class _PingSession:
    """Minimal session stand-in: records ping calls, answers each ping per
    the injected behaviour (return or raise)."""

    def __init__(self, ping: Callable[[], Awaitable[mcp_types.EmptyResult]]) -> None:
        self._ping = ping
        self.ping_calls = 0

    async def send_ping(self) -> mcp_types.EmptyResult:
        self.ping_calls += 1
        return await self._ping()


async def _ok_ping() -> mcp_types.EmptyResult:
    return mcp_types.EmptyResult()


async def _run(
    session: _PingSession,
    op: Callable[[], Awaitable[object]],
    *,
    probe_interval: float = 0.05,
    ping_timeout: float = 0.05,
) -> object:
    return await dispatch_with_liveness(
        session,
        op,
        op_label="t",
        org_id="o",
        upstream_id="u",
        probe_interval=probe_interval,
        ping_timeout=ping_timeout,
    )


@pytest.mark.asyncio
async def test_fast_result_returns_without_pinging() -> None:
    session = _PingSession(_ok_ping)

    async def op() -> str:
        return "done"

    assert await _run(session, op) == "done"
    assert session.ping_calls == 0, "a fast op must never trigger a ping"


@pytest.mark.asyncio
async def test_slow_but_alive_op_is_not_torn_down() -> None:
    """R2: a tool slower than the probe interval whose server still answers
    pings runs to completion — it is NEVER torn down, so a non-idempotent
    tool can't be double-executed by a forced retry."""
    session = _PingSession(_ok_ping)

    async def op() -> str:
        await asyncio.sleep(0.25)  # spans several probe intervals
        return "slow-done"

    assert await _run(session, op) == "slow-done"
    assert session.ping_calls >= 1, "the slow op must have pinged for liveness"


@pytest.mark.asyncio
async def test_silent_stall_raises_timeout_and_cancels_op() -> None:
    """Op hangs AND ping hangs (the transport went silent) → TimeoutError,
    and the abandoned op is cancelled (no leak)."""
    cancelled = asyncio.Event()

    async def hang_ping() -> mcp_types.EmptyResult:
        await asyncio.sleep(3600)
        return mcp_types.EmptyResult()

    session = _PingSession(hang_ping)

    async def op() -> str:
        try:
            await asyncio.sleep(3600)
            return "never"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(asyncio.TimeoutError):
        await _run(session, op)
    assert session.ping_calls >= 1
    assert cancelled.is_set(), "the abandoned op must be cancelled on stall"


@pytest.mark.asyncio
async def test_op_closed_stream_propagates_without_pinging() -> None:
    """A promptly-raised closed/broken stream is the op's OWN exception —
    propagated unchanged so the caller classifies it as a stall. The ping
    path is only for a *silent* hang, not a prompt transport error."""
    session = _PingSession(_ok_ping)

    async def op() -> str:
        raise anyio.ClosedResourceError()

    with pytest.raises(anyio.ClosedResourceError):
        await _run(session, op)
    assert session.ping_calls == 0


@pytest.mark.asyncio
async def test_server_error_during_op_propagates() -> None:
    session = _PingSession(_ok_ping)

    async def op() -> str:
        raise McpError(mcp_types.ErrorData(code=-32603, message="boom"))

    with pytest.raises(McpError):
        await _run(session, op)


@pytest.mark.asyncio
async def test_silent_real_client_session_detected_by_ping() -> None:
    """Stronger than the mocked stall test (review item 10): a REAL MCP
    ``ClientSession`` whose peer goes SILENT — no response to the tool call
    OR the liveness ping — is detected via the ping path and raises
    TimeoutError.

    Exercises the real SDK ``send_request`` / ``send_ping`` / response-demux
    machinery over real anyio streams; only the peer is silent. That is
    exactly the E2B #1128 stall shape (a connected stream that delivers
    nothing), minus E2B's intermittent post-pause AUTO-RESUME — which makes a
    paused sandbox an unreliable way to reproduce a silent stall on the
    dispatch path, so the mechanism is pinned deterministically here instead.
    """
    import anyio
    from mcp.client.session import ClientSession

    # server→client stream: we hold the send end and NEVER send (silent).
    _to_client_send, to_client_recv = anyio.create_memory_object_stream(10)
    # client→server stream: the client writes here; buffer is large enough
    # that the (undrained) requests don't block.
    from_client_send, _from_client_recv = anyio.create_memory_object_stream(10)
    try:
        async with ClientSession(to_client_recv, from_client_send) as session:
            with pytest.raises(asyncio.TimeoutError):
                await dispatch_with_liveness(
                    session,
                    lambda: session.call_tool("anything", {}),
                    op_label="anything",
                    org_id="o",
                    upstream_id="u",
                    probe_interval=0.2,
                    ping_timeout=0.2,
                )
    finally:
        _to_client_send.close()
        _from_client_recv.close()


@pytest.mark.asyncio
async def test_ping_answered_with_error_keeps_waiting() -> None:
    """A ping the server ANSWERS (even with an error) proves the transport
    is alive → keep waiting, don't heal. Only a ping that times out / breaks
    the stream signals a silent stall."""

    async def err_ping() -> mcp_types.EmptyResult:
        raise McpError(mcp_types.ErrorData(code=-32601, message="no ping"))

    session = _PingSession(err_ping)

    async def op() -> str:
        await asyncio.sleep(0.18)
        return "ok"

    assert await _run(session, op) == "ok"
    assert session.ping_calls >= 1


# --- ROUTE-2 / ROUTE-10: op completion racing the in-flight ping --------------


class _SlowPingSession:
    """Session whose ping itself takes time to answer — used to exercise the
    window where the op finishes WHILE a liveness ping is in flight. The
    ``answers`` flag toggles a pong vs. a silent (timing-out) ping so one
    builder covers both the alive-but-slow ping (ROUTE-2) and the
    non-compliant silent-drop ping (ROUTE-10)."""

    def __init__(self, *, ping_delay: float, answers: bool) -> None:
        self._ping_delay = ping_delay
        self._answers = answers
        self.ping_calls = 0

    async def send_ping(self) -> mcp_types.EmptyResult:
        self.ping_calls += 1
        if self._answers:
            await asyncio.sleep(self._ping_delay)
            return mcp_types.EmptyResult()
        # Non-compliant: silently drops the ping — never answers.
        await asyncio.sleep(3600)
        return mcp_types.EmptyResult()


@pytest.mark.asyncio
async def test_op_completes_while_ping_in_flight_returns_no_heal() -> None:
    """ROUTE-2: the op finishes (~probe_interval + eps) WHILE a liveness ping
    is still being answered (the ping itself runs ~2× probe_interval). A
    compliant server answers that ping (pong = alive), so the loop keeps
    waiting and, on the next probe, finds the op done — the real result is
    returned, never a spurious stall/heal. The op is NOT torn down."""
    # ping answers, but slowly: ~2× probe_interval. ping_timeout is generous
    # so the pong lands before the timeout fires.
    session = _SlowPingSession(ping_delay=0.10, answers=True)
    op_completed = asyncio.Event()

    async def op() -> str:
        # ~probe_interval + eps: not done at the first probe, so a ping
        # fires; completes while that ping is still in flight.
        await asyncio.sleep(0.07)
        op_completed.set()
        return "done"

    result = await dispatch_with_liveness(
        session,
        op,
        op_label="t",
        org_id="o",
        upstream_id="u",
        probe_interval=0.05,
        ping_timeout=1.0,
    )
    assert result == "done", "the real result must be returned, not a stall"
    assert op_completed.is_set(), "the op must run to completion, not be torn down"
    assert session.ping_calls >= 1, "the slow op must have pinged for liveness"


@pytest.mark.asyncio
async def test_silent_ping_drop_spuriously_heals_a_live_slow_op() -> None:
    """ROUTE-10 (documented trade-off, NOT a bug): a NON-compliant upstream
    that silently drops the liveness ping is indistinguishable from a silent
    stall. A live-but-slow op (it WOULD complete at ~3× probe_interval) is
    therefore torn down and surfaced as a ``TimeoutError`` — the accepted
    false-positive called out in ``dispatch_with_liveness``'s R2 assumption
    note. This green test pins that current behavior so a future change to
    the ping contract is a conscious decision, not an accident."""
    session = _SlowPingSession(ping_delay=0.0, answers=False)
    op_completed = asyncio.Event()
    op_cancelled = asyncio.Event()

    async def op() -> str:
        try:
            # Would complete well after the first probe — a genuinely slow,
            # genuinely LIVE tool.
            await asyncio.sleep(0.15)
            op_completed.set()
            return "would-have-finished"
        except asyncio.CancelledError:
            op_cancelled.set()
            raise

    with pytest.raises(asyncio.TimeoutError):
        await dispatch_with_liveness(
            session,
            op,
            op_label="t",
            org_id="o",
            upstream_id="u",
            probe_interval=0.05,
            ping_timeout=0.05,
        )
    assert session.ping_calls >= 1
    assert op_cancelled.is_set(), (
        "the live-but-slow op is torn down — the documented false positive"
    )
    assert not op_completed.is_set(), (
        "the op never gets to finish, even though the server was alive"
    )
