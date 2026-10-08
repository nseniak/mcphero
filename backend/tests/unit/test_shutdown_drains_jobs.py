"""The shutdown closes the stores last: nothing that still writes to them
runs after they close.

The lifespan teardown used to stop the org runtimes and close Mongo while
background jobs (an admin's Start still refreshing tools, an admin action
a cancelled request left running, a sign-in warning) and MCP handlers
still ran: their writes failed (``audit.write_failed`` at every deploy)
or were lost; a Start recorded success for a server the shutdown had just
closed; a sandbox create a Stop abandoned landed after the close, kept
its sandbox but lost its ref, so the next boot created another one.

``shut_down`` now runs the steps in order (``lifecycle.ShutdownSteps``),
each runtime cancels its own jobs, every org and every session closes at
once, every held job (those included) is waited for
(``drain_every_set``) and none may start while the stores close.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from fastapi import FastAPI

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    STOP_ALL_WAIT_SECONDS,
    UpstreamClientManager,
)
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from mcpolis.domain.services.background_tasks import (
    BackgroundTaskSet,
    drain_every_set,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.upstream_admin_service import UpstreamAdminService
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.lifecycle import (
    DrainCoordinator,
    ShutdownBudget,
    ShutdownSteps,
    shut_down,
)
from tests.unit._state_seed import seed_user_session
from tests.unit.factories import (
    Gate,
    YieldingAuditRepository,
    make_runtime_manager,
    make_upstream_definition,
)
from tests.unit.fake_sandbox_service import make_fake_sandbox_service
from tests.unit.sandbox_e2b_mock import make_mock_e2b_client
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    _config_with_one_stdio,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
)
from tests.unit.test_mcp_endpoints_start_at_boot import make_standalone_settings
from tests.unit.test_sandbox_concurrency import make_choked_create
from tests.unit.test_sandbox_lifecycle_across_boots import (
    STABLE,
    make_boot,
    make_reconciler,
    open_and_close_preserved,
)


class ClosableStore(InMemorySandboxPersistenceRepository):
    """The sandbox-ref store as the lifespan sees it: once Mongo is closed,
    every call raises (pymongo: "Cannot use MongoClient after close")."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def _check(self) -> None:
        if self.closed:
            raise RuntimeError("Cannot use MongoClient after close")

    async def upsert(self, ref: SandboxPersistedRef) -> None:
        self._check()
        await super().upsert(ref)

    async def get(
        self, *, org_id: str, upstream_id: str,
    ) -> SandboxPersistedRef | None:
        self._check()
        return await super().get(org_id=org_id, upstream_id=upstream_id)

    async def delete(self, *, org_id: str, upstream_id: str) -> None:
        self._check()
        await super().delete(org_id=org_id, upstream_id=upstream_id)


class CloseWaits:
    """A connection whose close waits at ``gate``, and says when it began."""

    def __init__(self, gate: Gate) -> None:
        self._gate = gate
        self.closing = asyncio.Event()

    async def close(self) -> None:
        self.closing.set()
        await self._gate.hold()


class StopAllWaits(UpstreamClientManager):
    """An org runtime whose teardown waits at ``gate``, and says when it
    began."""

    def __init__(self, gate: Gate) -> None:
        super().__init__([])
        self._gate = gate
        self.stopping = asyncio.Event()

    async def stop_all(self, *, wait: float = STOP_ALL_WAIT_SECONDS) -> None:
        self.stopping.set()
        await self._gate.hold()
        await super().stop_all(wait=wait)


async def test_stop_all_cancels_and_waits_for_a_start_still_running(
    tmp_path: Path,
) -> None:
    """An admin's Start whose connect has landed (its state record lets
    go of it) is still running, here writing its audit row, when the
    shutdown stops the runtime: the runtime cancels it and waits for it,
    so nothing of it runs once the stores close."""
    cfg, mcps = _config_with_one_stdio()
    fake = make_fake_sandbox_service()
    manager = UpstreamClientManager(
        upstreams=[],
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": fake},
    )
    audit_gate = Gate()
    audit = YieldingAuditRepository(audit_gate)
    parts = await make_admin_parts(
        tmp_path, config=cfg, mcp_servers=mcps, client_manager=manager,
        audit_repo=audit,
    )
    service = UpstreamAdminService(parts.action_deps)

    with structlog.testing.capture_logs() as logs:
        outcome = await service.start_upstream(
            DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL,
        )
        started = outcome.started
        assert started is not None
        await asyncio.wait_for(audit_gate.reached.wait(), 5)

        await manager.stop_all()
        ended_with_the_runtime = started.done()
        audit_gate.release.set()
        await asyncio.sleep(0.05)

    assert ended_with_the_runtime, "the Start still ran after its runtime stopped"
    assert audit.rows == [], [row.action for row in audit.rows]
    errors = [e["event"] for e in logs if e.get("log_level") == "error"]
    assert errors == []


async def test_stop_all_closes_every_session_at_once() -> None:
    """Each close can take its full timeout: one by one, a few sessions
    outlasted the shutdown."""
    gate = Gate()
    manager = UpstreamClientManager([])
    closes = [CloseWaits(gate), CloseWaits(gate)]
    for user, close in zip(("a@x.com", "b@x.com"), closes, strict=True):
        seed_user_session(manager, "u1", user, task=close)  # type: ignore[arg-type]

    stopping = asyncio.create_task(manager.stop_all())
    try:
        await asyncio.wait_for(
            asyncio.gather(*(close.closing.wait() for close in closes)), 5,
        )
    finally:
        gate.release.set()
    await asyncio.wait_for(stopping, 5)


async def test_shutdown_all_stops_every_org_at_once() -> None:
    gate = Gate()
    first, second = StopAllWaits(gate), StopAllWaits(gate)
    runtimes = make_runtime_manager(
        PolicyEngine(SettingsConfig()), client_manager=first,
    )
    other = make_runtime_manager(
        PolicyEngine(SettingsConfig()), client_manager=second, org_id="other-org",
    )
    runtimes._runtimes["other-org"] = other._runtimes["other-org"]  # pyright: ignore[reportPrivateUsage]

    stopping = asyncio.create_task(runtimes.shutdown_all())
    try:
        await asyncio.wait_for(
            asyncio.gather(first.stopping.wait(), second.stopping.wait()), 5,
        )
    finally:
        gate.release.set()
    await asyncio.wait_for(stopping, 5)
    assert runtimes.all_runtimes == {}


def make_steps(
    events: list[str], jobs: BackgroundTaskSet, loop_task: asyncio.Task[object],
) -> ShutdownSteps:
    """Shutdown steps that record when they run. Stopping the runtimes
    leaves a job running (like a Start the runtime let go of); closing
    the stores tries to start one."""

    async def close_mcp_sessions() -> None:
        events.append(f"sessions closed, loop done={loop_task.done()}")

    def keep_sandboxes() -> None:
        events.append("sandboxes kept")

    async def late_job() -> None:
        await asyncio.sleep(0.05)
        events.append("late job done")

    async def stop_runtimes() -> None:
        events.append("runtimes stopped")
        jobs.spawn(late_job())

    async def flush() -> None:
        events.append("sign-ins flushed")

    async def job_after_the_close() -> None:
        events.append("a job ran while the stores closed")

    async def close_stores() -> None:
        jobs.spawn(job_after_the_close())
        await asyncio.sleep(0.05)
        events.append("stores closed")

    return ShutdownSteps(
        loops=[loop_task],
        close_mcp_sessions=close_mcp_sessions,
        keep_sandboxes=keep_sandboxes,
        stop_runtimes=stop_runtimes,
        flush_gateway_sign_ins=flush,
        close_stores=close_stores,
    )


async def test_the_shutdown_closes_the_stores_last() -> None:
    events: list[str] = []
    jobs = BackgroundTaskSet()
    loop_task = asyncio.create_task(asyncio.Event().wait())
    drain = DrainCoordinator(drain_timeout=1.0)

    await shut_down(
        drain, make_steps(events, jobs, loop_task),
        ShutdownBudget(background_jobs=5.0),
    )

    assert events == [
        "sandboxes kept",
        "sessions closed, loop done=True",
        "runtimes stopped",
        "late job done",
        "sign-ins flushed",
        "stores closed",
    ]
    # New jobs are accepted again once the shutdown is over.
    assert not jobs.refuses_new_jobs


async def test_the_real_shutdown_waits_for_every_held_job(tmp_path: Path) -> None:
    """Whatever component holds a background job, the app's shutdown
    waits for it before it closes the stores."""
    app: FastAPI = create_app(make_standalone_settings(tmp_path))
    jobs = BackgroundTaskSet()
    finished = asyncio.Event()

    async def job() -> None:
        await asyncio.sleep(0.2)
        finished.set()

    async with app.router.lifespan_context(app):
        jobs.spawn(job())

    assert finished.is_set(), "the shutdown returned before the job ended"


async def test_a_sandbox_kept_at_shutdown_is_reused_at_the_next_boot() -> None:
    """A Start's sandbox create is in flight when the shutdown aborts the
    connect. The create lands while the shutdown still waits, records the
    sandbox it keeps, and only then do the stores close: the next boot
    reuses that sandbox instead of creating another one."""
    store = ClosableStore()
    client = make_mock_e2b_client()
    upstream = make_upstream_definition(id="ups-late", command="npx")
    service = make_boot(client, store)
    manager = UpstreamClientManager(
        [upstream],
        org_id="acme",
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": service},
        sandbox_persistence=store,
        mcpolis_instance=STABLE,
    )
    gate, arrived = asyncio.Event(), asyncio.Event()
    client.create_sandbox = make_choked_create(  # type: ignore[method-assign]
        client, gate=gate, arrived=arrived,
    )
    connect = asyncio.create_task(manager.connect_upstream(upstream))
    await asyncio.wait_for(arrived.wait(), 5)

    # The shutdown: sandboxes kept, the runtime stopped (it gives up
    # waiting on the create quickly here), every held job drained, and
    # only then the stores closed.
    service.mark_all_active_sessions_preserve_on_close()
    await manager.stop_all(wait=0.05)
    draining = asyncio.create_task(drain_every_set(5.0, unwind_timeout=1.0))
    await asyncio.sleep(0.05)
    gate.set()  # the create lands while the shutdown still waits
    await asyncio.wait_for(draining, 10)
    store.closed = True
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
