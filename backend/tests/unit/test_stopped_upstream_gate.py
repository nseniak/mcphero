"""A stopped MCP server stays stopped until an admin clicks Start.

Stop marks the upstream DISABLED. Anything else that would open its
shared session again (a tool call's lazy attach, a heal, a delayed tool
refresh) is refused, so a sandbox cannot start behind the admin's back.
Only for servers without per-user sign-in (service_account).

Driven by ``FakeSandboxService``: a real MCP server over memory streams,
so ``session_open_count`` counts the sandboxes actually opened.
"""
import asyncio
from pathlib import Path

import pytest
import structlog

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
    UpstreamStopped,
)
from mcpolis.adapters.upstream_clients.upstream_state import (
    UpstreamConnectionState,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.upstream_connection_service import (
    UPSTREAM_STOPPED,
    SessionUnavailable,
    acquire_upstream_session,
)
from tests.unit.factories import make_upstream_definition
from tests.unit.fake_sandbox_service import (
    FakeSandboxService,
    make_fake_sandbox_service,
)
from tests.unit._shared_session_harness import make_manager
from tests.unit._user_session_harness import (
    ConnectionGate,
    make_upstream,
    start_upstream,
    stop_upstream,
)


async def make_stopped_server() -> tuple[
    UpstreamClientManager, FakeSandboxService,
]:
    """A server that was running until the admin clicked Stop."""
    fake = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    await mgr.connect_upstream(upstream)
    await mgr.disconnect_upstream(upstream.id)
    return mgr, fake


def state_of(mgr: UpstreamClientManager) -> UpstreamConnectionState:
    state = mgr.get_state("everything2")
    assert state is not None
    return state.state


@pytest.mark.asyncio
async def test_a_tool_call_does_not_start_a_stopped_mcp_server() -> None:
    mgr, fake = await make_stopped_server()
    upstream = mgr.get_upstream("everything2")
    assert upstream is not None
    try:
        opened_before = fake.session_open_count

        outcome = await asyncio.gather(
            mgr.ensure_shared_connected(upstream), return_exceptions=True,
        )

        assert state_of(mgr) == UpstreamConnectionState.DISABLED, (
            "a tool call restarted a stopped MCP server"
        )
        assert fake.session_open_count == opened_before, (
            "a tool call opened a sandbox for a stopped MCP server"
        )
        assert isinstance(outcome[0], UpstreamStopped), outcome
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_tool_call_on_a_stopped_server_is_refused_quietly() -> None:
    """The caller gets a clean "not available", and nothing is logged as
    an error: the refusal is what the admin asked for, not a fault."""
    mgr, _ = await make_stopped_server()
    upstream = mgr.get_upstream("everything2")
    assert upstream is not None
    try:
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(SessionUnavailable) as refused:
                await acquire_upstream_session(
                    org_id=DEFAULT_ORG_ID,
                    upstream=upstream,
                    effective_user="",
                    connection_store=None,
                    client_manager=mgr,
                    server_url="http://localhost:8000",
                )
        assert refused.value.reason == UPSTREAM_STOPPED
        errors = [e for e in logs if e.get("log_level") == "error"]
        assert errors == [], f"a refused tool call was logged as an error: {errors}"
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_heal_does_not_restart_a_stopped_mcp_server() -> None:
    """A call that stalled just before Stop heals afterwards. The heal
    must not bring the server back."""
    mgr, fake = await make_stopped_server()
    upstream = mgr.get_upstream("everything2")
    assert upstream is not None
    try:
        opened_before = fake.session_open_count

        with pytest.raises(UpstreamStopped):
            await mgr.reconnect_shared_fresh(upstream)

        assert state_of(mgr) == UpstreamConnectionState.DISABLED
        assert fake.session_open_count == opened_before
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_start_still_opens_a_stopped_mcp_server() -> None:
    """The dashboard's Start marks the upstream CONNECTING, then
    connects. That is the one way back in."""
    mgr, _ = await make_stopped_server()
    upstream = mgr.get_upstream("everything2")
    assert upstream is not None
    try:
        async def start() -> None:
            await mgr.connect_upstream(upstream)

        task = asyncio.create_task(start())
        mgr.register_background_connect_task(upstream.id, task)
        await asyncio.wait_for(task, timeout=10)

        assert state_of(mgr) == UpstreamConnectionState.LIVE
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_tool_call_still_retries_a_failed_mcp_server() -> None:
    """FAILED is not stopped: a server that failed (at boot, or on an
    earlier call) is retried by the next tool call."""
    fake = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    try:
        await mgr.transition_to_failed(
            upstream.id, last_failure="connect timed out",
            reason="boot_connect_failed",
        )

        await asyncio.wait_for(mgr.ensure_shared_connected(upstream), timeout=10)

        assert state_of(mgr) == UpstreamConnectionState.LIVE
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_the_stop_gate_leaves_sign_in_servers_alone(tmp_path: Path) -> None:
    """Servers with per-user sign-in are exempt: their tool calls run on
    each user's own session, and after an admin signs in again the
    upstream can still read DISABLED until the next restart."""
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)  # per_user_oauth
    mgr = UpstreamClientManager([upstream])
    try:
        await mgr.transition_to_disabled(upstream.id)

        await asyncio.wait_for(mgr.connect_shared(upstream), timeout=10)

        state = mgr.get_state(upstream.id)
        assert state is not None and state.state == UpstreamConnectionState.LIVE
    finally:
        await mgr.stop_all()
        await stop_upstream(server, server_task)
