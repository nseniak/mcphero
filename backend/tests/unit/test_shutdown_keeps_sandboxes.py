"""A sandbox alive when a deploy starts is kept for the next boot, even
when the shutdown cuts the connect that was starting it.

``shut_down`` marks the sandboxes to keep FIRST (``keep_sandboxes``),
before it cancels the loops and closes the MCP sessions. It used to mark
them after. A connect whose only waiter was a gateway tool call (its
handler is cancelled when the sessions close) or one of the app's loops
(the boot connect, a heal) was then abandoned before the mark: its
sandbox session closed unmarked and killed the sandbox, and the next boot
created it again, the cold start the keep is for. A loop's abandoned
connect closes its session on its own, so the mark is only too late when
the shutdown waits meanwhile: for the sessions, or for another loop that
takes a moment to stop.

A connect that the runtimes' teardown aborts while its sandbox is still
being created is waited for by the shutdown's job drain, so the sandbox
it keeps is recorded before the stores close.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.entrypoints.lifecycle import (
    DrainCoordinator,
    ShutdownBudget,
    ShutdownSteps,
    shut_down,
)
from tests.unit.factories import make_upstream_definition
from tests.unit.sandbox_e2b_mock import MockE2BClient, make_mock_e2b_client
from tests.unit.test_sandbox_concurrency import make_choked_create
from tests.unit.test_sandbox_lifecycle_across_boots import (
    STABLE,
    make_boot,
    make_reconciler,
    open_and_close_preserved,
)
from tests.unit.test_shutdown_drains_jobs import ClosableStore


def make_manager(
    upstream: UpstreamDefinition,
    service: E2BSandboxService,
    store: InMemorySandboxPersistenceRepository,
) -> UpstreamClientManager:
    return UpstreamClientManager(
        [upstream],
        org_id="acme",
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": service},
        sandbox_persistence=store,
        mcpolis_instance=STABLE,
    )


def make_budget() -> ShutdownBudget:
    """The production budget's shape, scaled down."""
    return ShutdownBudget(
        loops=1.0, mcp_sessions=1.0, background_jobs=2.0, unwind=0.5,
        gateway_flush=0.5, stores=0.5,
    )


async def nothing() -> None:
    return None


def make_steps(
    *,
    service: E2BSandboxService,
    manager: UpstreamClientManager,
    loops: Sequence[asyncio.Task[Any]] = (),
    close_mcp_sessions: Callable[[], Awaitable[None]] = nothing,
    close_stores: Callable[[], Awaitable[None]] = nothing,
) -> ShutdownSteps:
    """The lifespan's steps for one org runtime and one sandbox service."""

    def keep_sandboxes() -> None:
        service.mark_all_active_sessions_preserve_on_close()

    return ShutdownSteps(
        loops=loops,
        close_mcp_sessions=close_mcp_sessions,
        keep_sandboxes=keep_sandboxes,
        stop_runtimes=lambda: manager.stop_all(wait=0),
        flush_gateway_sign_ins=nothing,
        close_stores=close_stores,
    )


async def make_drained() -> DrainCoordinator:
    """The request drain, already over, as after the SIGTERM drain."""
    drain = DrainCoordinator(drain_timeout=0.1)
    await drain.drain()
    return drain


async def start_cold_connect(
    client: MockE2BClient,
    manager: UpstreamClientManager,
    upstream: UpstreamDefinition,
) -> asyncio.Task[object]:
    """A connect whose sandbox exists while its MCP is still starting: the
    mock never answers the MCP handshake, like a package download that
    has not finished. The returned task is its only waiter."""
    waiter: asyncio.Task[object] = asyncio.create_task(
        manager.ensure_shared_connected(upstream),
    )
    await asyncio.wait_for(client.run_command_started.wait(), 5)
    await asyncio.sleep(0.05)  # the MCP handshake is under way
    return waiter


async def test_a_sandbox_whose_connect_a_closing_session_cuts_is_kept() -> None:
    """The connect's only waiter is a gateway tool call: closing the MCP
    sessions cancels its handler, which abandons the connect."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-cold", command="npx")
    service = make_boot(client, store)
    manager = make_manager(upstream, service, store)
    tool_call = await start_cold_connect(client, manager, upstream)

    async def close_mcp_sessions() -> None:
        # What leaving the gateway's session manager does to the call.
        tool_call.cancel()
        await asyncio.gather(tool_call, return_exceptions=True)
        await asyncio.sleep(0.1)

    await shut_down(
        await make_drained(),
        make_steps(
            service=service, manager=manager,
            close_mcp_sessions=close_mcp_sessions,
        ),
        make_budget(),
    )

    assert [kill.sandbox_id for kill in client.kills] == [], (
        "closing the MCP sessions abandoned a connect before the sandboxes "
        "were marked to be kept: its sandbox was killed"
    )
    assert len(client.live_infos) == 1


async def test_a_sandbox_whose_connect_a_cancelled_loop_cuts_is_kept() -> None:
    """The connect's only waiter is one of the app's loops (the boot
    connect, a liveness heal): the shutdown cancels it."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-boot", command="npx")
    service = make_boot(client, store)
    manager = make_manager(upstream, service, store)
    boot_connect = await start_cold_connect(client, manager, upstream)

    async def close_mcp_sessions() -> None:
        await asyncio.sleep(0.1)  # what leaving the endpoints takes

    await shut_down(
        await make_drained(),
        make_steps(
            service=service, manager=manager, loops=[boot_connect],
            close_mcp_sessions=close_mcp_sessions,
        ),
        make_budget(),
    )

    assert [kill.sandbox_id for kill in client.kills] == [], (
        "cancelling the loops abandoned the boot connect before the "
        "sandboxes were marked to be kept: its sandbox was killed"
    )
    assert len(client.live_infos) == 1


async def a_loop_whose_stop_takes_a_moment() -> None:
    """A periodic loop whose cancel waits for a cleanup to end, as a loop
    releasing its lock in the store does."""
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0.5)


async def test_a_sandbox_kept_while_a_slow_loop_stops_is_not_killed() -> None:
    """The boot connect's only waiter is cancelled with the other loops,
    and one of those takes a moment to stop. The shutdown waits for it
    (up to ``ShutdownBudget.loops``), and meanwhile the abandoned connect
    closes its sandbox session. Marked to be kept only once the loops had
    stopped, that close killed the sandbox: the mark must come first."""
    store = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-boot", command="npx")
    service = make_boot(client, store)
    manager = make_manager(upstream, service, store)
    boot_connect = await start_cold_connect(client, manager, upstream)
    slow_loop = asyncio.create_task(a_loop_whose_stop_takes_a_moment())
    await asyncio.sleep(0)  # the loop is waiting for its next step

    await shut_down(
        await make_drained(),
        make_steps(
            service=service, manager=manager, loops=[boot_connect, slow_loop],
        ),
        make_budget(),
    )

    assert [kill.sandbox_id for kill in client.kills] == [], (
        "the boot connect, abandoned while another loop stopped, killed "
        "its sandbox before the sandboxes were marked to be kept"
    )
    assert len(client.live_infos) == 1


async def test_a_sandbox_created_while_the_shutdown_waits_for_jobs_is_reused() -> None:
    """A Start's sandbox create is in flight when the runtimes' teardown
    aborts its connect, and lands while the job drain waits: the sandbox
    it keeps is recorded before the stores close, and the next boot
    reuses it instead of creating another."""
    store = ClosableStore()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-late", command="npx")
    service = make_boot(client, store)
    manager = make_manager(upstream, service, store)
    gate, arrived = asyncio.Event(), asyncio.Event()
    client.create_sandbox = make_choked_create(  # type: ignore[method-assign]
        client, gate=gate, arrived=arrived,
    )
    connect = asyncio.create_task(manager.connect_upstream(upstream))
    await asyncio.wait_for(arrived.wait(), 5)

    async def close_stores() -> None:
        store.closed = True

    async def create_lands_later() -> None:
        await asyncio.sleep(0.5)  # the runtimes have stopped by now
        gate.set()

    landing = asyncio.create_task(create_lands_later())
    await shut_down(
        await make_drained(),
        make_steps(service=service, manager=manager, close_stores=close_stores),
        make_budget(),
    )
    await landing
    await asyncio.gather(connect, return_exceptions=True)
    del client.create_sandbox  # back to the mock's own create

    # Next boot, Mongo back.
    store.closed = False
    await make_reconciler(client, store).reconcile()
    await open_and_close_preserved(make_boot(client, store), upstream, "next")

    assert len(client.creates) == 1, (
        "the sandbox kept at shutdown was not reused: the next boot created "
        f"{len(client.creates) - 1} more"
    )
