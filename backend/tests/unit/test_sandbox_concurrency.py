"""Sandbox concurrency guardrails (SBX-CONC-1..4).

These pin the race-safety of ``E2BSandboxService``'s process-local
bookkeeping (``_live_sandboxes`` / ``_session_owners`` /
``_preserve_on_close``) and the cleanup of an in-flight ``session()``
create that a cancel interrupts.

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

import anyio
import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService
from mcpolis.adapters.sandbox_e2b.client import E2BSandboxHandle, E2BSDKError
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


# ---------- SBX-CONC-4: a create in flight ----------
#
# The reconcile used to spare the sandbox of a create in flight, through
# a "creating" record written before each create. The reconcile runs
# only at boot, before any create, so that record only ever protected
# the sandbox of a start a crash had cut short (see
# ``test_sandbox_lifecycle_across_boots.py``). What a create in flight
# still owes is its own cleanup when it fails, which no cancel may cut.


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
    fires once the sandbox is provider-visible.
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


async def open_and_close(
    service: E2BSandboxService, upstream_id: str,
) -> None:
    """Open a session of ``upstream_id`` for org ``acme``, close it."""
    async with service.session(
        session_id=f"session-{upstream_id}",
        org_id="acme",
        upstream=make_upstream_definition(id=upstream_id, command="npx"),
        resources=make_default_resources(),
        denylist=(),
    ):
        pass


@pytest.mark.asyncio
async def test_a_failed_start_cancelled_during_its_kill_still_kills_the_sandbox() -> None:
    """The MCP command fails to start in a fresh sandbox, and a cancel (a
    Stop, a shutdown) lands while that sandbox is being killed. Nothing
    else knows the sandbox, so the kill must go through anyway, and the
    cancel must still end the start. (Review of the follow-up fixes,
    F4: the kill used to be cut short and the sandbox left running.)"""
    persistence = InMemorySandboxPersistenceRepository()
    service, client = make_reuse_e2b_service(persistence=persistence)
    client.run_command_raises = E2BSDKError("E2BSDKError", "failed to start")
    client.kill_gate = asyncio.Event()

    opening = asyncio.create_task(open_and_close(service, "ups-failed"))
    await asyncio.wait_for(client.kill_started.wait(), timeout=5)
    opening.cancel()  # lands while E2B is still answering the kill
    client.kill_gate.set()
    outcome = await asyncio.gather(opening, return_exceptions=True)

    assert isinstance(outcome[0], asyncio.CancelledError), outcome
    assert [k.sandbox_id for k in client.kills] == ["sbx-0"], (
        "the cancel cut the kill of a sandbox nothing else knows about"
    )
    assert await persistence.get(org_id="acme", upstream_id="ups-failed") is None


@pytest.mark.asyncio
async def test_a_start_cancelled_by_an_anyio_scope_still_kills_its_sandbox() -> None:
    """Same, with the cancel coming from an anyio cancel scope while the
    MCP command is starting. Such a scope delivers its cancel again at
    every await, so the cleanup must hold it off until the kill is done,
    then let it end the start."""
    persistence = InMemorySandboxPersistenceRepository()
    service, client = make_reuse_e2b_service(persistence=persistence)
    client.run_command_gate = asyncio.Event()  # the command never starts
    client.kill_gate = asyncio.Event()
    scopes: list[anyio.CancelScope] = []

    async def open_in_scope() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await open_and_close(service, "ups-anyio")

    opening = asyncio.create_task(open_in_scope())
    await asyncio.wait_for(client.run_command_started.wait(), timeout=5)
    scopes[0].cancel()
    await asyncio.wait_for(client.kill_started.wait(), timeout=5)
    client.kill_gate.set()
    await asyncio.wait_for(opening, timeout=5)

    assert scopes[0].cancelled_caught, "the scope's cancel must end the start"
    assert [k.sandbox_id for k in client.kills] == ["sbx-0"], (
        "the scope's repeated cancel cut the kill of a stranded sandbox"
    )
    assert await persistence.get(org_id="acme", upstream_id="ups-anyio") is None


@pytest.mark.asyncio
async def test_session_opened_after_the_shutdown_mark_is_not_killed() -> None:
    """The shutdown cleanup marks live sessions, then closes runtimes org
    by org. A connect still in flight (boot reattach, wake) can register
    its sandbox after the mark; when ``stop_all`` cancels it, it must be
    preserved like the others, not killed with its ref deleted (which the
    old SIGKILL-on-deploy never did)."""
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_reuse_e2b_service(persistence=persistence)
    gate = asyncio.Event()
    arrived = asyncio.Event()
    mock.create_sandbox = make_choked_create(  # type: ignore[method-assign]
        mock, gate=gate, arrived=arrived,
    )
    opened = asyncio.Event()
    upstream = make_upstream_definition(id="ups-late", command="npx")

    async def connect_in_flight() -> None:
        async with service.session(
            session_id="late",
            org_id="acme",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            opened.set()
            await asyncio.Event().wait()  # held until stop_all cancels it

    flight = asyncio.create_task(connect_in_flight())
    await arrived.wait()

    # The shutdown mark sees nothing live yet.
    assert service.mark_all_active_sessions_preserve_on_close() == 0

    # The connect lands during the runtime shutdown, then is cancelled.
    gate.set()
    await opened.wait()
    flight.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flight

    assert mock.kills == [], f"a sandbox opened during shutdown was killed: {mock.kills}"
    assert await persistence.get(org_id="acme", upstream_id="ups-late") is not None
