"""SandboxConnectionTask pause/resume + persistence round-trip.

Step 9 of the rollout: pause() persists the SnapshotRef via
SandboxPersistenceRepository, and the next ``start()`` reads the ref
back and passes it as ``resume_from`` to ``service.session()``.

Uses the E2B mock client (since E2B is the only backend with real
pause support today) + InMemorySandboxPersistenceRepository.
"""
from __future__ import annotations

import asyncio

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService
from mcpolis.adapters.upstream_clients.stdio_adapter import (
    SandboxConnectionTask,
)
from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition
from tests.unit.sandbox_e2b_mock import MockE2BClient, make_mock_e2b_client


def make_default_resources() -> SandboxResources:
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


def make_e2b_setup() -> tuple[
    E2BSandboxService,
    MockE2BClient,
    InMemorySandboxPersistenceRepository,
]:
    mock = make_mock_e2b_client()
    service = E2BSandboxService(mock, mcpolis_instance="instance-A", on_timeout_seconds=60)
    persistence = InMemorySandboxPersistenceRepository()
    return service, mock, persistence


def make_paused_task(
    *,
    service: E2BSandboxService,
    persistence: InMemorySandboxPersistenceRepository,
    upstream_id: str = "ups-pause",
) -> SandboxConnectionTask:
    upstream = make_upstream_definition(id=upstream_id, command="npx")
    return SandboxConnectionTask(
        upstream,
        user_id="__shared__",
        service=service,
        resources=make_default_resources(),
        org_id="acme",
        sandbox_persistence=persistence,
        mcpolis_instance="instance-A",
    )


@pytest.mark.asyncio
async def test_pause_persists_snapshot_ref() -> None:
    """A successful pause writes a ``SandboxPersistedRef`` to the
    repository, keyed by (org_id, upstream_id), with the provider
    name + snapshot id from the SnapshotRef the service returned."""
    service, mock, persistence = make_e2b_setup()
    task = make_paused_task(
        service=service, persistence=persistence, upstream_id="ups-pause-1",
    )
    # Drive the task via _run() rather than .start() so we don't
    # block on session-future fulfillment that requires a real MCP
    # initialize. The mock SDK doesn't speak MCP, so initialize()
    # would hang. _run() drives the SandboxService.session() lifecycle
    # we want to exercise; the session_future side-effect is expected
    # to fail with a timeout — fine for this test.
    run_task = asyncio.create_task(task._run())  # type: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)
    # Sanity: the service has a live registered handle.
    assert task._session_id in service._live_sandboxes  # type: ignore[reportPrivateUsage]

    ref = await task.pause()
    assert ref is not None
    assert ref.provider == "e2b"

    # Persistence captured the same id; metadata carries the original
    # sandbox id; mcpolis_instance pinned for reconciler safety.
    persisted = await persistence.get(
        org_id="acme", upstream_id="ups-pause-1",
    )
    assert persisted is not None
    assert persisted.provider == "e2b"
    assert persisted.paused_snapshot_id == ref.snapshot_id
    assert persisted.sandbox_id is None
    assert persisted.mcpolis_instance == "instance-A"
    assert "original_sandbox_id" in persisted.metadata

    # Drive the session out of context to release the run task; it
    # may complete with an exception due to mock not speaking MCP.
    task._shutdown_event.set()  # type: ignore[reportPrivateUsage]
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        run_task.cancel()
    _ = mock  # keep the reference alive for clarity.


@pytest.mark.asyncio
async def test_pause_returns_none_when_persistence_unset() -> None:
    """No persistence repository ⇒ pause still returns the SnapshotRef
    (the service-level pause completed) but the upstream-level
    persistence write is skipped."""
    service, _mock, _persistence = make_e2b_setup()
    upstream = make_upstream_definition(id="ups-no-persist", command="npx")
    task = SandboxConnectionTask(
        upstream,
        user_id="__shared__",
        service=service,
        resources=make_default_resources(),
        org_id="acme",
        sandbox_persistence=None,  # explicit
        mcpolis_instance="instance-A",
    )
    run_task = asyncio.create_task(task._run())  # type: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)
    ref = await task.pause()
    assert ref is not None
    task._shutdown_event.set()  # type: ignore[reportPrivateUsage]
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        run_task.cancel()


@pytest.mark.asyncio
async def test_resume_from_persisted_ref_calls_connect() -> None:
    """A second task picks up where the first paused: reads the
    persisted ref, passes it as ``resume_from`` to
    ``service.session()``, which routes through Sandbox.connect()."""
    service, mock, persistence = make_e2b_setup()

    # First task: open + pause.
    task1 = make_paused_task(
        service=service, persistence=persistence, upstream_id="ups-resume",
    )
    run1 = asyncio.create_task(task1._run())  # type: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)
    await task1.pause()
    task1._shutdown_event.set()  # type: ignore[reportPrivateUsage]
    try:
        await asyncio.wait_for(run1, timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        run1.cancel()

    creates_so_far = len(mock.creates)
    connects_so_far = len(mock.connects)

    # Second task: same (org, upstream). Should resume.
    task2 = make_paused_task(
        service=service, persistence=persistence, upstream_id="ups-resume",
    )
    run2 = asyncio.create_task(task2._run())  # type: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)
    # The second open must have called connect, not create.
    assert len(mock.creates) == creates_so_far
    assert len(mock.connects) == connects_so_far + 1
    task2._shutdown_event.set()  # type: ignore[reportPrivateUsage]
    try:
        await asyncio.wait_for(run2, timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        run2.cancel()


@pytest.mark.asyncio
async def test_provider_mismatch_in_persistence_drops_ref() -> None:
    """If the operator switched providers, a stored ref from the old
    provider isn't honored: it gets deleted and the next session
    cold-starts on the new backend."""
    from datetime import datetime, timezone

    from mcpolis.domain.ports.sandbox_persistence_repository import (
        SandboxPersistedRef,
    )

    service, mock, persistence = make_e2b_setup()
    # Plant a ref from a different provider.
    await persistence.upsert(SandboxPersistedRef(
        provider="own-runner",  # mismatched
        org_id="acme",
        upstream_id="ups-mismatch",
        mcpolis_instance="instance-A",
        sandbox_id=None,
        paused_snapshot_id="old-runner-snap",
        pid=None,
        metadata={},
        cached_server_info=None,
        cached_self_description=None,
        last_updated=datetime.now(tz=timezone.utc),
    ))

    upstream = make_upstream_definition(id="ups-mismatch", command="npx")
    task = SandboxConnectionTask(
        upstream,
        user_id="__shared__",
        service=service,
        resources=make_default_resources(),
        org_id="acme",
        sandbox_persistence=persistence,
        mcpolis_instance="instance-A",
    )
    run_task = asyncio.create_task(task._run())  # type: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)
    # The mismatched ref must have been dropped from persistence,
    # and the session must have cold-started (Sandbox.create) rather
    # than tried Sandbox.connect("old-runner-snap").
    persisted = await persistence.get(
        org_id="acme", upstream_id="ups-mismatch",
    )
    assert persisted is None
    assert len(mock.creates) >= 1
    assert len(mock.connects) == 0
    task._shutdown_event.set()  # type: ignore[reportPrivateUsage]
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        run_task.cancel()


