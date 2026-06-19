"""Smoke test for :class:`FakeSandboxService`.

Proves the in-memory test double behaves like a real sandbox backend
from a wrapping ``ClientSession``'s POV, and that each control knob
produces the failure shape the router/manager recovery paths classify.
"""
# NOTE: no ``from __future__ import annotations`` — FastMCP tool
# registration calls issubclass() on annotations, which breaks under
# stringified annotations.

import asyncio

import pytest
from mcp.client.session import ClientSession

from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition
from tests.unit.fake_sandbox_service import make_fake_sandbox_service


def make_resources() -> SandboxResources:
    """First valid grid combo for the fake (always validates)."""
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


@pytest.mark.asyncio
async def test_real_round_trip_initialize_list_and_call() -> None:
    """(a) A wrapping ClientSession over a fresh FakeSandboxService
    session completes initialize + tools/list + a tools/call."""
    service = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="fake-mcp", command="ignored")

    async with service.session(
        session_id="sess-1",
        org_id="org",
        upstream=upstream,
        resources=make_resources(),
        denylist=(),
    ) as sandbox_session:
        async with ClientSession(
            sandbox_session.read_stream, sandbox_session.write_stream,
        ) as session:
            init_result = await session.initialize()
            assert init_result.serverInfo.name == "FakeUpstream"

            tools = await session.list_tools()
            assert "echo" in [t.name for t in tools.tools]

            result = await session.call_tool("echo", {"message": "hi"})
            assert not result.isError
            assert "echo:hi" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stall_makes_a_call_hang() -> None:
    """(b) ``stall()`` makes a subsequent call hang — asserted with a
    short asyncio timeout (the dispatch-path liveness ping bounds this
    in production; here we just prove the silence)."""
    service = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="fake-mcp", command="ignored")

    async with service.session(
        session_id="sess-1",
        org_id="org",
        upstream=upstream,
        resources=make_resources(),
        denylist=(),
    ) as sandbox_session:
        async with ClientSession(
            sandbox_session.read_stream, sandbox_session.write_stream,
        ) as session:
            await session.initialize()

            handle = service.last_session
            assert handle is not None
            handle.stall()

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    session.call_tool("echo", {"message": "after-stall"}),
                    timeout=0.5,
                )


@pytest.mark.asyncio
async def test_kill_sets_transport_failed_and_reports_not_alive() -> None:
    """(c) ``kill()`` sets ``transport_failed`` and the session reports
    not-alive."""
    service = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="fake-mcp", command="ignored")

    async with service.session(
        session_id="sess-1",
        org_id="org",
        upstream=upstream,
        resources=make_resources(),
        denylist=(),
    ) as sandbox_session:
        assert sandbox_session.transport_failed is not None
        assert not sandbox_session.transport_failed.is_set()

        handle = service.last_session
        assert handle is not None
        assert handle.is_alive

        handle.kill()

        assert sandbox_session.transport_failed.is_set()
        assert not handle.is_alive


@pytest.mark.asyncio
async def test_session_open_counter_increments_per_session() -> None:
    """(d) The session-open counter increments once per ``session()``
    context entry."""
    service = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="fake-mcp", command="ignored")

    assert service.session_open_count == 0

    for i in range(3):
        async with service.session(
            session_id=f"sess-{i}",
            org_id="org",
            upstream=upstream,
            resources=make_resources(),
            denylist=(),
        ):
            pass

    assert service.session_open_count == 3
    assert len(service.sessions) == 3


@pytest.mark.asyncio
async def test_fire_exit_resolves_the_exit_signal() -> None:
    """Bonus knob: ``fire_exit`` resolves the per-session ExitSignal so
    the connection task's init-exit race fires."""
    service = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="fake-mcp", command="ignored")

    async with service.session(
        session_id="sess-1",
        org_id="org",
        upstream=upstream,
        resources=make_resources(),
        denylist=(),
    ) as sandbox_session:
        handle = service.last_session
        assert handle is not None
        handle.fire_exit(exit_code=42, stderr_tail="boom")

        # ExitSignal.wait() now resolves immediately.
        await asyncio.wait_for(sandbox_session.exit_signal.wait(), timeout=0.5)
        snap = sandbox_session.exit_signal.snapshot()
        assert snap.exit_code == 42
        assert "boom" in snap.stderr_tail
