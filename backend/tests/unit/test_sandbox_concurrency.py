"""Sandbox concurrency guardrails (SBX-CONC-1..4).

These pin the race-safety of ``E2BSandboxService``'s process-local
bookkeeping (``_live_sandboxes`` / ``_session_owners`` /
``_preserve_on_close``) and the boot reconciler against an in-flight
``session()`` create.

Determinism is paramount: NO real sleeps for synchronisation. Where a
test needs two coroutines to interleave at a precise point, it injects
an ``asyncio.Event`` choke point into the mock SDK so the interleaving
is driven by the test, not by wall-clock timing.

Reuses ``make_e2b_service`` + the mock client builders from
``test_e2b_sandbox_service.py`` and ``sandbox_e2b_mock.py``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxReconciler, E2BSandboxService
from mcpolis.adapters.sandbox_e2b.client import E2BSandboxHandle
from tests.unit.factories import make_upstream_definition
from tests.unit.sandbox_e2b_mock import MockE2BClient, make_mock_e2b_client
from tests.unit.test_e2b_sandbox_service import (
    make_default_resources,
    make_e2b_service,
)


def make_reuse_e2b_service(
    *,
    persistence: InMemorySandboxPersistenceRepository,
    mcpolis_instance: str = "test-instance",
    client: MockE2BClient | None = None,
) -> tuple[E2BSandboxService, MockE2BClient]:
    """``E2BSandboxService`` with reuse-on-restart enabled so it writes
    persistence refs on session entry (SBX-CONC-3 needs this to assert
    refs survive a preserve-on-close teardown)."""
    real_client = client if client is not None else make_mock_e2b_client()
    service = E2BSandboxService(
        real_client,
        mcpolis_instance=mcpolis_instance,
        on_timeout_seconds=60,
        persistence=persistence,
        volumes_enabled=False,
        reuse_sandboxes_on_restart=True,
    )
    return service, real_client


# ---------- SBX-CONC-1: pause() racing session teardown ----------


@pytest.mark.asyncio
async def test_pause_racing_session_teardown_is_consistent() -> None:
    """A ``pause(session_id)`` call firing at the exact moment the
    session context exits must not double-kill, must not raise, and
    must leave the bookkeeping consistent.

    The ``_session_cm`` finally block pops ``_live_sandboxes`` FIRST
    ("so concurrent pause() returns None safely"), so exactly one of
    {pause, teardown} wins the live handle. Whichever wins, the other
    is a clean no-op: no exception escapes, and ``_live_sandboxes`` is
    empty afterwards. A pause that wins suppresses the kill (the
    snapshot is the new state); a teardown that wins kills exactly
    once. Either way, never two kills.
    """
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-race", command="npx")

    # An event the two racing coroutines both wait on, so they're
    # released into the contended window together (deterministic
    # contention without wall-clock timing).
    start = asyncio.Event()
    pause_result: list[object] = []
    pause_error: list[BaseException] = []

    async def race_pause() -> None:
        await start.wait()
        try:
            pause_result.append(await service.pause(session_id="race"))
        except BaseException as exc:  # noqa: BLE001
            pause_error.append(exc)

    async with service.session(
        session_id="race",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pause_task = asyncio.create_task(race_pause())
        # Release the racer right before the context exits — it runs
        # its ``pause()`` concurrently with the finally block.
        start.set()
        await asyncio.sleep(0)  # let the racer reach ``pause()``
    # Context has exited (teardown ran). Let the racer finish.
    await pause_task

    # No exception escaped the racing pause.
    assert pause_error == [], f"pause raised under teardown race: {pause_error}"
    # Bookkeeping is fully clean: nothing left registered.
    assert service._live_sandboxes == {}  # type: ignore[reportPrivateUsage]
    assert service._session_owners == {}  # type: ignore[reportPrivateUsage]
    assert "race" not in service._preserve_on_close  # type: ignore[reportPrivateUsage]
    # Never a double-kill on the same sandbox.
    killed_ids = [k.sandbox_id for k in mock.kills]
    assert len(killed_ids) == len(set(killed_ids)), (
        f"a sandbox was killed more than once: {killed_ids}"
    )
    assert len(mock.kills) <= 1, f"at most one kill expected, got {mock.kills}"


# ---------- SBX-CONC-2: N distinct concurrent sessions ----------


@pytest.mark.asyncio
async def test_n_concurrent_distinct_sessions_dont_collide() -> None:
    """10 concurrent ``session()`` contexts with distinct session ids
    against ONE service register and tear down independently — no id
    clobbers another's ``_live_sandboxes`` / ``_session_owners`` /
    ``_preserve_on_close`` entry, and every sandbox is killed exactly
    once on a clean (non-preserve) exit."""
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_reuse_e2b_service(persistence=persistence)
    n = 10

    # A barrier so all sessions are simultaneously live before any
    # tears down — proves the registries hold N entries at once.
    all_open = asyncio.Event()
    open_count = {"n": 0}

    async def run_session(i: int) -> None:
        upstream = make_upstream_definition(id=f"ups-{i}", command="npx")
        async with service.session(
            session_id=f"sess-{i}",
            org_id="acme",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            open_count["n"] += 1
            if open_count["n"] == n:
                all_open.set()
            # Hold the session open until every sibling is registered.
            await all_open.wait()
            # Peak-concurrency invariant: this id is registered, and
            # the live count equals the number opened so far.
            assert f"sess-{i}" in service._live_sandboxes  # type: ignore[reportPrivateUsage]

    await asyncio.gather(*(run_session(i) for i in range(n)))

    # All torn down independently — nothing lingering.
    assert service._live_sandboxes == {}  # type: ignore[reportPrivateUsage]
    assert service._session_owners == {}  # type: ignore[reportPrivateUsage]
    assert service._preserve_on_close == {}  # type: ignore[reportPrivateUsage]
    # N distinct creates, N distinct kills (clean exit kills each).
    assert len(mock.creates) == n
    killed_ids = [k.sandbox_id for k in mock.kills]
    assert len(killed_ids) == len(set(killed_ids)) == n, (
        f"each of {n} sandboxes must be killed exactly once; got {killed_ids}"
    )
    # Every (org, upstream) ref was deleted on the clean kill path.
    for i in range(n):
        assert await persistence.get(org_id="acme", upstream_id=f"ups-{i}") is None


# ---------- SBX-CONC-3: parallel preserve-on-close teardown ----------


@pytest.mark.asyncio
async def test_parallel_preserve_teardown_keeps_all_refs() -> None:
    """``mark_all_active_sessions_preserve_on_close`` then a concurrent
    teardown of every session: zero sandboxes killed, every persistence
    ref preserved (so the next boot can reattach). This is the graceful-
    shutdown (SIGTERM / deploy) path — sandboxes must outlive the
    process."""
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_reuse_e2b_service(persistence=persistence)
    n = 8

    all_open = asyncio.Event()
    teardown = asyncio.Event()
    open_count = {"n": 0}

    async def run_session(i: int) -> None:
        upstream = make_upstream_definition(id=f"ups-{i}", command="npx")
        async with service.session(
            session_id=f"sess-{i}",
            org_id="acme",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            open_count["n"] += 1
            if open_count["n"] == n:
                all_open.set()
            # Hold open until the test marks preserve + signals teardown.
            await teardown.wait()

    tasks = [asyncio.create_task(run_session(i)) for i in range(n)]
    await all_open.wait()

    # Mark every live session preserve-on-close, then release them all
    # to tear down concurrently.
    marked = service.mark_all_active_sessions_preserve_on_close()
    assert marked == n
    teardown.set()
    await asyncio.gather(*tasks)

    # Preserve path: NOT a single kill.
    assert mock.kills == [], f"preserve-on-close must not kill: {mock.kills}"
    # Every ref survived for the next boot's reconnect.
    for i in range(n):
        ref = await persistence.get(org_id="acme", upstream_id=f"ups-{i}")
        assert ref is not None, f"ref for ups-{i} must be preserved"
        assert ref.sandbox_id is not None and ref.pid is not None
    # Bookkeeping cleared after teardown regardless.
    assert service._live_sandboxes == {}  # type: ignore[reportPrivateUsage]
    assert service._preserve_on_close == {}  # type: ignore[reportPrivateUsage]


# ---------- SBX-CONC-4 [BUG?]: reconciler racing an in-flight create ----------


def make_choked_create(
    mock: MockE2BClient, *, gate: asyncio.Event, arrived: asyncio.Event,
) -> Callable[..., Awaitable[E2BSandboxHandle]]:
    """Wrap ``mock.create_sandbox`` so it registers the sandbox in the
    provider's view (``live_infos``) and then BLOCKS on ``gate`` before
    returning the handle to the service.

    This reproduces the real in-flight window: on E2B, the sandbox
    exists provider-side (and the reconciler's ``list_sandboxes`` can
    see it) the instant ``create`` is issued — but the service hasn't
    yet returned from ``create`` to run ``_persist_live_ref``. ``arrived``
    fires once the sandbox is provider-visible so the test can run the
    reconcile in exactly that gap.
    """
    real_create = mock.create_sandbox

    async def choked_create(**kwargs: object) -> E2BSandboxHandle:
        # ``real_create`` appends to ``live_infos`` synchronously before
        # any await, so the sandbox is provider-visible the moment this
        # returns its handle. We must make it visible BEFORE we block,
        # so call through, then hold the handle behind the gate.
        handle = await real_create(**kwargs)  # type: ignore[arg-type]
        arrived.set()
        await gate.wait()
        return handle

    return choked_create


@pytest.mark.asyncio
async def test_reconciler_does_not_kill_in_flight_create() -> None:
    """INTENDED contract: a sandbox that ``session()`` has created but
    not yet persisted a live ref for must NOT be killed by a reconcile
    that races into that window.

    Setup: choke ``create_sandbox`` so the sandbox is provider-visible
    (in ``list_sandboxes``) but the service is suspended before
    ``_persist_live_ref`` runs. Fire the reconcile in that gap. The
    in-flight sandbox is tagged with our instance and is ``running`` and
    is not yet in persistence — the reconciler's current heuristic
    classifies that as an orphan and kills it. The intended behaviour is
    to leave an actively-creating sandbox alone, so this is RED until
    the create/persist/reconcile race is closed.
    """
    instance = "instance-A"
    persistence = InMemorySandboxPersistenceRepository()
    mock = make_mock_e2b_client()
    service = E2BSandboxService(
        mock,
        mcpolis_instance=instance,
        on_timeout_seconds=60,
        persistence=persistence,
        volumes_enabled=False,
        reuse_sandboxes_on_restart=True,
    )
    reconciler = E2BSandboxReconciler(
        mock, persistence, mcpolis_instance=instance,
    )

    gate = asyncio.Event()
    arrived = asyncio.Event()
    mock.create_sandbox = make_choked_create(  # type: ignore[method-assign]
        mock, gate=gate, arrived=arrived,
    )

    upstream = make_upstream_definition(id="ups-inflight", command="npx")

    async def open_session() -> None:
        async with service.session(
            session_id="inflight",
            org_id="acme",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            # Preserve so the (eventual) clean exit doesn't itself kill
            # the sandbox — we're isolating the reconciler's behaviour.
            service.mark_session_preserve_on_close("inflight")

    session_task = asyncio.create_task(open_session())
    # Wait until the sandbox is provider-visible but the service is
    # still suspended inside the choked create (pre-persist).
    await arrived.wait()

    # Race the reconcile into the in-flight window.
    report = await reconciler.reconcile()

    # Release the create so the session can finish cleanly.
    gate.set()
    await session_task

    # INTENDED: the in-flight sandbox is left alone.
    assert report.killed_orphan_sandboxes == 0, (
        "reconciler killed an in-flight (creating, not-yet-persisted) "
        f"sandbox; kills={mock.kills}"
    )
    assert mock.kills == []
