"""Router/dispatch-level concurrency guardrails: an in-flight MCP request
must survive a session being torn down underneath it (idle sweep, stall
eviction) by surfacing a CLEAN, classifiable stall — never a hang or an
unhandled crash.

Both tests run the in-flight call through the PRODUCTION dispatch path
(``dispatch_with_liveness``) over a REAL ``ClientSession`` built by the
manager on a ``FakeSandboxService`` session. The fake's ``stall()`` / the
sweep's session ``close()`` make the transport go silent mid-call; the
dispatch's ping-gated liveness probe is what converts that silence into a
bounded ``asyncio.TimeoutError`` (which ``is_transport_stall`` classifies →
heal). Tiny probe/ping intervals keep the tests sub-second; they pin the
BEHAVIOUR, not the production timings.

NOTE: no ``from __future__ import annotations`` — FastMCP tool registration
calls ``issubclass()`` on annotations (see ``fake_sandbox_service.py``).
"""
import asyncio
import time

import pytest
from mcp.server.fastmcp import FastMCP

from mcpolis.adapters.upstream_clients.client_manager import (
    USER_SESSION_IDLE_TIMEOUT,
    UpstreamClientManager,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.tool_router import dispatch_with_liveness
from tests.unit.factories import make_upstream_definition
from tests.unit.fake_sandbox_service import (
    FakeSandboxService,
    make_fake_sandbox_service,
)


def make_manager(
    upstream: UpstreamDefinition, fake: FakeSandboxService,
) -> UpstreamClientManager:
    return UpstreamClientManager(
        upstreams=[upstream],
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": fake},
    )


def make_slow_tool_factory(
    entered: asyncio.Event, release: asyncio.Event,
):
    """A ``server_factory`` with a ``slow`` tool that signals ``entered``
    when the server starts running it and then blocks on ``release`` — an
    in-process Event choke point so a call can be held genuinely in-flight
    on the server side, with no real sleep."""

    def factory() -> FastMCP:
        server = FastMCP(name="SlowUpstream")

        @server.tool(name="slow", description="A slow tool")
        async def slow(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
            entered.set()
            await release.wait()
            return f"slow:{message}"

        @server.tool(name="echo", description="Echo")
        def echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
            return f"echo:{message}"

        return server

    return factory


# --- CONC-5: shared-session eviction during an in-flight call -------------


@pytest.mark.asyncio
async def test_shared_session_stall_then_reconnect_yields_fresh_session() -> None:
    """CONC-5: a shared service_account session that goes silent mid-call is
    bounded by ``dispatch_with_liveness`` into a ``TimeoutError`` (not a
    hang); a subsequent ``reconnect_shared_fresh`` then yields a DIFFERENT,
    fully-usable session — no unhandled exception anywhere.

    This is the dispatch-side of the stall-heal contract over real
    transports: ``stall()`` is the E2B #1128 silent-stall shape, the ping
    probe detects it, and the fresh reconnect lands the next call on a
    clean session."""
    fake = make_fake_sandbox_service()  # default echo server
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    await mgr.connect_shared(upstream)
    session = mgr.get_session("everything2")
    handle = fake.last_session
    assert handle is not None

    # Silence the transport, then dispatch through the production path.
    handle.stall()
    with pytest.raises(asyncio.TimeoutError):
        await dispatch_with_liveness(
            session,
            lambda: session.call_tool("echo", {"message": "x"}),
            op_label="echo",
            org_id="o",
            upstream_id="everything2",
            probe_interval=0.1,
            ping_timeout=0.1,
        )

    # Heal: a fresh reconnect must open a NEW session.
    await mgr.reconnect_shared_fresh(upstream)
    assert fake.session_open_count == 2, "the heal opens a fresh sandbox"

    fresh = mgr.get_session("everything2")
    assert fresh is not session, "the healed session is a different object"
    result = await fresh.call_tool("echo", {"message": "after-heal"})
    assert not result.isError
    assert "echo:after-heal" in result.content[0].text  # type: ignore[union-attr]

    await mgr.stop_all()


# --- CONC-4: idle sweep vs an in-flight dispatch --------------------------


@pytest.mark.asyncio
async def test_idle_sweep_during_inflight_dispatch_surfaces_clean_stall() -> None:
    """CONC-4: when the idle sweep closes a per-user session while a tool
    call is in flight ON THAT SESSION, the dispatch must surface a CLEAN
    stall — never hang.

    The call runs through the production ``dispatch_with_liveness`` path
    (exactly as ``ToolRouter._dispatch_with_recovery`` runs it). The sweep's
    ``task.close()`` tears the transport down out from under the in-flight
    op; the dispatch's liveness ping then sees the now-closed stream and
    raises a bounded ``asyncio.TimeoutError`` (the in-flight "pin" that
    keeps a sweep-induced teardown from hanging the caller). Awaiting the
    dispatch directly inside ``pytest.raises`` is the no-hang assertion: a
    genuine hang would never return and the test would time out.

    Determinism: the call is held server-side on an Event, the session is
    pushed past the idle threshold by writing its ``last_used`` directly
    (no wall-clock sleep), and ONE sweep tick is fired by calling
    ``_sweep_idle_sessions`` directly rather than via the real-sleep loop.
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_slow_tool_factory(entered, release),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)

    # A per-user session is the idle-swept kind (shared/admin are exempt).
    await mgr.connect_upstream_for_user(upstream, user_id="alice@co.com")
    session = mgr.get_session("everything2", user_id="alice@co.com")

    dispatch = asyncio.create_task(
        dispatch_with_liveness(
            session,
            lambda: session.call_tool("slow", {"message": "x"}),
            op_label="slow",
            org_id="o",
            upstream_id="everything2",
            probe_interval=0.1,
            ping_timeout=0.1,
        )
    )
    # The tool is now running on the session — the call is genuinely
    # in-flight.
    await asyncio.wait_for(entered.wait(), timeout=5.0)

    # Push past the idle threshold deterministically and fire ONE sweep
    # tick directly (no reliance on the real ``asyncio.sleep`` loop).
    key = ("alice@co.com", "everything2")
    mgr._user_session_last_used[key] = (  # pyright: ignore[reportPrivateUsage]
        time.monotonic() - USER_SESSION_IDLE_TIMEOUT - 1
    )
    await mgr._sweep_idle_sessions()  # pyright: ignore[reportPrivateUsage]
    assert key not in mgr._user_sessions, (  # pyright: ignore[reportPrivateUsage]
        "the sweep closed the idle per-user session"
    )

    # The in-flight dispatch must surface a clean stall, NOT hang. Awaiting
    # it directly (no outer wait_for) makes a hang fail by never returning.
    with pytest.raises(asyncio.TimeoutError):
        await dispatch

    # Let the orphaned server-side tool unblock so teardown is clean.
    release.set()
    await mgr.stop_all()
