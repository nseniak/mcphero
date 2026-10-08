"""A gateway request the MCP SDK cuts off still leaves its audit row.

The SDK cancels a request handler through an anyio cancel scope: on the
client's ``notifications/cancelled``, and when the session's transport
closes (a gateway sign-in revoke, a member removal and the end of the
shutdown drain all close it). anyio raises that cancellation again at
every ``await`` until the scope exits, so an audit write that waits on
the network, as Mongo's insert does, used to be cancelled too and the
row was lost.

These tests drive the real SDK client and server over its in-memory
transport, with an audit store whose write waits like Mongo's.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import MagicMock

import anyio
import mcp.types as mcp_types
import structlog
from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from structlog.typing import EventDict

from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import (
    AUDIT_WRITE_TIMEOUT_SECONDS,
    ToolRouter,
)
from mcpolis.entrypoints.controllers.gateway_controller import create_mcp_server
from tests.unit.factories import (
    Gate,
    YieldingAuditRepository,
    make_discovered_tool,
    make_full_access_config,
    make_runtime_manager,
    make_upstream_definition,
)
from tests.unit.stall_client_manager_fake import StallClientManagerFake

UpstreamCall = Callable[..., Awaitable[mcp_types.CallToolResult]]

# Request 0 of an in-memory session is ``initialize``; the first tool
# call is request 1.
FIRST_CALL_ID = 1


async def answer_ok(*_args: Any, **_kwargs: Any) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")],
    )


def make_hanging_call() -> tuple[UpstreamCall, asyncio.Event]:
    """An upstream call that never answers, and the event it sets once
    it is running."""
    started = asyncio.Event()

    async def hang(*_args: Any, **_kwargs: Any) -> mcp_types.CallToolResult:
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError("the upstream call was never cut off")

    return hang, started


def make_gateway(
    upstream_call: UpstreamCall,
    audit: YieldingAuditRepository,
    *,
    allowed_upstreams: list[str] | None = None,
    audit_write_timeout_seconds: float = AUDIT_WRITE_TIMEOUT_SECONDS,
) -> Server[Any, Any]:
    """The gateway over one service-account MCP ``mee6`` with one tool,
    ``do_thing``, whose calls run ``upstream_call``. The caller (the
    in-memory session signs nobody in, so "anonymous") may use the MCPs
    in ``allowed_upstreams``, by default ``mee6``."""
    upstream = make_upstream_definition(id="mee6")
    session = MagicMock()
    session.call_tool = upstream_call
    client_manager = StallClientManagerFake(session)
    registry = ToolRegistry([upstream], cast(Any, client_manager))
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(upstream_id="mee6", original_name="do_thing"),
    ]
    router = ToolRouter(
        registry, cast(Any, client_manager), audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
        audit_write_timeout_seconds=audit_write_timeout_seconds,
    )
    allowed = ["mee6"] if allowed_upstreams is None else allowed_upstreams
    runtime_manager = make_runtime_manager(
        PolicyEngine(make_full_access_config(allowed, ["anonymous"])),
        tool_registry=registry,
        tool_router=router,
    )
    return create_mcp_server(runtime_manager)


async def call_until_cut_off(client: ClientSession, cut_off: asyncio.Event) -> None:
    """Call ``mee6__do_thing``; set ``cut_off`` when the gateway answers
    that the request was cancelled."""
    try:
        await client.call_tool("mee6__do_thing", {})
    except McpError:
        cut_off.set()


async def cancel_request(client: ClientSession, request_id: int) -> None:
    await client.send_notification(
        mcp_types.ClientNotification(
            mcp_types.CancelledNotification(
                method="notifications/cancelled",
                params=mcp_types.CancelledNotificationParams(requestId=request_id),
            ),
        ),
    )


async def test_client_cancel_still_leaves_one_audit_row() -> None:
    upstream_call, started = make_hanging_call()
    audit = YieldingAuditRepository()
    server = make_gateway(upstream_call, audit)
    cut_off = asyncio.Event()

    async with asyncio.timeout(10):
        async with create_connected_server_and_client_session(server) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(call_until_cut_off, client, cut_off)
                await started.wait()
                await cancel_request(client, FIRST_CALL_ID)
                await cut_off.wait()
        # Leaving the session waits for the server's handlers to end.

    assert len(audit.rows) == 1, f"{len(audit.rows)} audit rows for one cancelled call"
    assert audit.rows[0].response_status == "cancelled"


async def test_closing_the_gateway_session_still_leaves_one_audit_row() -> None:
    """What a revoke, a member removal or the end of the shutdown drain
    does to a call in flight: the session's transport closes under it."""
    upstream_call, started = make_hanging_call()
    audit = YieldingAuditRepository()
    server = make_gateway(upstream_call, audit)

    async with asyncio.timeout(10):
        async with create_connected_server_and_client_session(server) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(call_until_cut_off, client, asyncio.Event())
                await started.wait()
                tg.cancel_scope.cancel()
        # Leaving the session closes its transport: the server's run loop
        # ends and cancels the call still in flight.

    assert len(audit.rows) == 1, (
        f"{len(audit.rows)} audit rows for one call cut off by a session close"
    )
    assert audit.rows[0].response_status == "cancelled"


async def test_a_cancel_that_lands_while_the_row_is_written_keeps_the_row_and_the_session() -> None:
    """The call has already succeeded when the client cancels it, and the
    cancel lands while the row is being written. The row records the
    success, and the gateway must not answer the cancelled request a
    second time: the SDK fails that answer and drops the whole session."""
    gate = Gate()
    audit = YieldingAuditRepository(gate=gate)
    server = make_gateway(answer_ok, audit)
    cut_off = asyncio.Event()

    async with asyncio.timeout(10):
        async with create_connected_server_and_client_session(server) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(call_until_cut_off, client, cut_off)
                await gate.reached.wait()
                await cancel_request(client, FIRST_CALL_ID)
                await cut_off.wait()
                gate.release.set()
            tools = await client.list_tools()

    assert [tool.name for tool in tools.tools] == ["mee6__do_thing"]
    assert [row.response_status for row in audit.rows] == ["success"]


async def test_a_refused_calls_row_survives_a_cancel_and_keeps_the_session() -> None:
    """Same race on a call the policy refuses: its ``denied`` row too."""
    gate = Gate()
    audit = YieldingAuditRepository(gate=gate)
    server = make_gateway(answer_ok, audit, allowed_upstreams=[])
    cut_off = asyncio.Event()

    async with asyncio.timeout(10):
        async with create_connected_server_and_client_session(server) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(call_until_cut_off, client, cut_off)
                await gate.reached.wait()
                await cancel_request(client, FIRST_CALL_ID)
                await cut_off.wait()
                gate.release.set()
            tools = await client.list_tools()

    assert tools.tools == []
    assert [row.response_status for row in audit.rows] == ["denied"]


async def wait_for_log_line(
    logs: list[EventDict], event: str, timeout_seconds: float,
) -> bool:
    """Whether a log line named ``event`` shows up within the time."""
    with anyio.move_on_after(timeout_seconds):
        while not any(line["event"] == event for line in logs):
            await asyncio.sleep(0.01)
        return True
    return False


async def test_a_hung_audit_store_cannot_hold_a_cancelled_call_forever() -> None:
    """The write is shielded from the cancel, so only its own time limit
    ends a write that never finishes. The lost row is logged as an
    ERROR, which Sentry captures."""
    gate = Gate()  # not opened in time: the store doesn't answer
    audit = YieldingAuditRepository(gate=gate)
    server = make_gateway(answer_ok, audit, audit_write_timeout_seconds=0.2)
    cut_off = asyncio.Event()

    with structlog.testing.capture_logs() as logs:
        async with asyncio.timeout(10):
            async with create_connected_server_and_client_session(server) as client:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(call_until_cut_off, client, cut_off)
                    await gate.reached.wait()
                    await cancel_request(client, FIRST_CALL_ID)
                    await cut_off.wait()
                gave_up = await wait_for_log_line(logs, "audit.write_failed", 5)
                # Let a write that never gave up end, so the test fails
                # on the assert below instead of hanging on the shield.
                gate.release.set()

    assert gave_up, "the shielded write never gave up on the hung store"
    lost = [line for line in logs if line["event"] == "audit.write_failed"]
    assert len(lost) == 1
    assert lost[0]["timed_out_after_seconds"] == 0.2
    assert lost[0]["tool"] == "mee6__do_thing"
    assert audit.rows == []
