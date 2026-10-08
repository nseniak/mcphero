"""The shutdown closes every MCP endpoint's sessions at once, and no
step holds it past its budget.

Leaving an endpoint's session manager cancels every handler of its
sessions and waits for them: a gateway tool call writes its audit row
(up to ``AUDIT_WRITE_TIMEOUT_SECONDS``), an Admin MCP call waits for its
action (up to ``WAIT_AFTER_CANCEL_SECONDS``). The lifespan used to leave
the session managers one after the other, in one ``AsyncExitStack``, so
those waits added up (15 s, against the 10 s docker-compose.yml gave the
step), and nothing in ``shut_down`` bounded the step, nor the store
close.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcpolis.domain.services.tool_router import (
    _write_audit_row,  # pyright: ignore[reportPrivateUsage]
)
from mcpolis.entrypoints.controllers.admin_tool_calls import (
    install_call_tool_wrapper,
)
from mcpolis.entrypoints.lifecycle import (
    DrainCoordinator,
    McpEndpoints,
    ShutdownBudget,
    ShutdownSteps,
    shut_down,
)
from mcpolis.entrypoints.mcp_transport_security import mcp_transport_security
from tests.unit._mcp_http_calls import call_tool_over_http
from tests.unit.factories import Gate, YieldingAuditRepository, make_audit_entry

# Scaled down from the real bounds (5 s and 10 s): one after the other
# they take 2.5 s, at once 1.5 s.
GATEWAY_AUDIT_SECONDS = 1.0
ADMIN_WAIT_AFTER_CANCEL_SECONDS = 1.5


def make_gateway_like_endpoint(gate: Gate) -> FastMCP:
    """A tool call held upstream, which writes its audit row on its way
    out, as the gateway's does, into a store that never answers."""
    server = FastMCP(
        name="gateway", streamable_http_path="/",
        transport_security=mcp_transport_security(),
    )
    stuck_audit = YieldingAuditRepository(Gate())  # never released

    @server.tool()
    async def upstream_tool() -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            await gate.hold()  # the upstream is still working
        finally:
            await _write_audit_row(
                stuck_audit, "acme", make_audit_entry(), GATEWAY_AUDIT_SECONDS,
            )
        return "done"

    return server


def make_admin_like_endpoint(gate: Gate) -> FastMCP:
    """An Admin MCP action held in a store that never answers, behind
    the real ``tools/call`` wrapper."""
    server = FastMCP(
        name="admin", streamable_http_path="/",
        transport_security=mcp_transport_security(),
    )

    @server.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def stop_mcp() -> str:  # pyright: ignore[reportUnusedFunction]
        await gate.hold()
        return "stopped"

    install_call_tool_wrapper(
        server, None, wait_after_cancel=ADMIN_WAIT_AFTER_CANCEL_SECONDS,
    )
    return server


def make_budget(**seconds: float) -> ShutdownBudget:
    """The production budget's shape, scaled down."""
    scaled = {
        "loops": 0.5, "mcp_sessions": 0.5, "background_jobs": 2.0,
        "unwind": 0.5, "gateway_flush": 0.5, "stores": 0.5,
    }
    return ShutdownBudget(**(scaled | seconds))


async def nothing() -> None:
    return None


def make_recording_steps(
    events: list[str],
    *,
    close_mcp_sessions: Gate | None = None,
    close_stores: Gate | None = None,
) -> ShutdownSteps:
    """Shutdown steps that record when they end. The step given a gate
    waits there."""

    async def held_until(gate: Gate | None, event: str) -> None:
        if gate is not None:
            await gate.hold()
        events.append(event)

    async def stop_runtimes() -> None:
        events.append("runtimes stopped")

    return ShutdownSteps(
        loops=[],
        close_mcp_sessions=lambda: held_until(close_mcp_sessions, "sessions closed"),
        keep_sandboxes=lambda: None,
        stop_runtimes=stop_runtimes,
        flush_gateway_sign_ins=nothing,
        close_stores=lambda: held_until(close_stores, "stores closed"),
    )


async def make_drained() -> DrainCoordinator:
    drain = DrainCoordinator(drain_timeout=0.1)
    await drain.drain()
    return drain


async def test_the_mcp_endpoints_close_at_once() -> None:
    admin_gate, gateway_gate = Gate(), Gate()
    gateway = make_gateway_like_endpoint(gateway_gate)
    admin = make_admin_like_endpoint(admin_gate)
    gateway_app, admin_app = gateway.streamable_http_app(), admin.streamable_http_app()
    endpoints = McpEndpoints([gateway.session_manager, admin.session_manager])
    await endpoints.start()
    calls = [
        asyncio.create_task(call_tool_over_http(gateway_app, "upstream_tool")),
        asyncio.create_task(call_tool_over_http(admin_app, "stop_mcp")),
    ]
    await asyncio.wait_for(admin_gate.reached.wait(), 10)
    await asyncio.wait_for(gateway_gate.reached.wait(), 10)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(endpoints.close(), 10)
    took = loop.time() - started

    admin_gate.release.set()
    gateway_gate.release.set()
    await asyncio.gather(*calls, return_exceptions=True)
    one_after_the_other = GATEWAY_AUDIT_SECONDS + ADMIN_WAIT_AFTER_CANCEL_SECONDS
    assert took >= ADMIN_WAIT_AFTER_CANCEL_SECONDS - 0.1, took
    assert took < one_after_the_other - 0.3, (
        f"closing the MCP endpoints took {took:.1f} s: they were left one "
        f"after the other ({one_after_the_other:.1f} s), not at once"
    )


async def test_a_session_close_past_its_budget_is_left_to_the_job_drain() -> None:
    """The runtimes stop once the sessions step's budget is over, and the
    stores still close only after the sessions did."""
    events: list[str] = []
    sessions_gate = Gate()

    async def sessions_end_during_the_job_drain() -> None:
        await asyncio.wait_for(sessions_gate.reached.wait(), 5)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(3):  # never hang the test on it
                while "runtimes stopped" not in events:
                    await asyncio.sleep(0.01)
        sessions_gate.release.set()

    releasing = asyncio.create_task(sessions_end_during_the_job_drain())
    with structlog.testing.capture_logs() as logs:
        await shut_down(
            await make_drained(),
            make_recording_steps(events, close_mcp_sessions=sessions_gate),
            make_budget(mcp_sessions=0.2),
        )
    await releasing

    assert events == ["runtimes stopped", "sessions closed", "stores closed"]
    assert [
        line["step"] for line in logs if line["event"] == "app.shutdown.step_timed_out"
    ] == ["mcp_sessions"]


async def test_a_store_close_that_hangs_does_not_hold_the_shutdown() -> None:
    events: list[str] = []
    stores_gate = Gate()
    loop = asyncio.get_running_loop()
    started = loop.time()

    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(shut_down(
            await make_drained(),
            make_recording_steps(events, close_stores=stores_gate),
            make_budget(stores=0.2),
        ), 5)
    took = loop.time() - started
    stores_gate.release.set()
    await asyncio.sleep(0.05)

    assert took < 1.0, took
    assert [
        line["step"] for line in logs if line["event"] == "app.shutdown.step_timed_out"
    ] == ["stores"]


async def test_a_loop_still_running_past_its_budget_is_left_to_the_job_drain() -> None:
    """A loop that holds on to its cancel for a while (a token refresh
    finishing its save) is waited for before the stores close."""
    events: list[str] = []
    refresh_saved = asyncio.Event()

    async def refresh_loop() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.4)  # the refresh's save
            events.append("refresh saved")
            refresh_saved.set()

    loop_task = asyncio.create_task(refresh_loop())
    await asyncio.sleep(0)
    await shut_down(
        await make_drained(),
        dataclasses.replace(make_recording_steps(events), loops=[loop_task]),
        make_budget(loops=0.1),
    )

    assert refresh_saved.is_set()
    assert events.index("refresh saved") < events.index("stores closed"), events


async def test_endpoints_closed_again_return_at_once() -> None:
    """The lifespan's own exit closes the endpoints a second time; once
    the shutdown has, that must not wait for them again."""
    gate = Gate()
    admin = make_admin_like_endpoint(gate)
    app = admin.streamable_http_app()
    endpoints = McpEndpoints([admin.session_manager])
    await endpoints.start()
    call = asyncio.create_task(call_tool_over_http(app, "stop_mcp"))
    await asyncio.wait_for(gate.reached.wait(), 10)
    first_close = asyncio.create_task(endpoints.close())
    await asyncio.sleep(0.1)

    await asyncio.wait_for(endpoints.close(), 0.5)

    gate.release.set()
    await asyncio.wait_for(first_close, 10)
    await asyncio.gather(call, return_exceptions=True)
