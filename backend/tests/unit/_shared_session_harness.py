"""Harness for shared-session races on ``FakeSandboxService``: a REAL
MCP server over memory streams, whose startup a gate can hold so a
connect is provably in flight.

NOTE: no ``from __future__ import annotations`` — FastMCP tool
registration calls ``issubclass()`` on annotations.
"""
import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from tests.unit.fake_sandbox_service import FakeSandboxService

def make_manager(
    upstream: UpstreamDefinition, fake: FakeSandboxService,
) -> UpstreamClientManager:
    """Wire a real manager to the in-memory fake under the ``e2b`` provider
    (the fake reports ``name = "e2b"`` so it stands in for the production
    backend in provider-keyed lookups)."""
    return UpstreamClientManager(
        upstreams=[upstream],
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": fake},
    )

def make_gated_server_factory(
    gate: asyncio.Event,
    hold: Callable[[int], bool] = lambda index: index == 1,
) -> Callable[[], FastMCP]:
    """A ``server_factory`` whose FIRST session blocks inside its lifespan
    until ``gate`` is set, holding that session's ``initialize`` (hence the
    in-flight ``connect_shared``) parked. Every later session starts
    immediately, so a coalesced sibling that opened its own session would
    still complete — making a single-flight regression visible as a
    ``session_open_count`` > 1 rather than a deadlock.

    ``hold`` picks the sessions to park by their open number (1-based);
    pass ``lambda _: True`` to park every session.
    """
    calls = 0

    def factory() -> FastMCP:
        nonlocal calls
        calls += 1
        index = calls

        @asynccontextmanager
        async def lifespan(_server: FastMCP):  # type: ignore[no-untyped-def]
            if hold(index):
                await gate.wait()
            yield {}

        server = FastMCP(name="GatedUpstream", lifespan=lifespan)

        @server.tool(name="echo", description="Echo back the message")
        def echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
            return f"echo:{message}"

        return server

    return factory

async def wait_until(predicate: Callable[[], bool]) -> None:
    """Yield to the event loop until ``predicate`` holds. Deterministic —
    spins on ``asyncio.sleep(0)`` (no wall-clock sleep), so it advances the
    loop without introducing a timing dependency."""
    while not predicate():
        await asyncio.sleep(0)


def lose_cancels_while_connecting(mgr: UpstreamClientManager) -> None:
    """Make every connect of ``mgr`` swallow cancels while it opens its
    transport, as a teardown on the way can (the E2B teardown finishes its
    cleanup through a cancel). Stands in for "the abort's cancel never
    reached the connect"."""
    real_create = mgr._create_task  # pyright: ignore[reportPrivateUsage]

    async def create_losing_cancels(*args: Any, **kwargs: Any) -> Any:
        inner = asyncio.ensure_future(real_create(*args, **kwargs))
        while True:
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                continue

    mgr._create_task = create_losing_cancels  # type: ignore[method-assign]
