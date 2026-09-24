"""A connect that is cancelled while it starts must close what it opened
at once: the sandbox for a stdio MCP, the connection for an HTTP one.

It used to keep going in the background until the MCP server finished
starting (or its start timed out, up to 120 s), then close itself. For E2B
that is a sandbox created and running for nobody. It also means Stop
returned, and killed the saved sandbox, while the aborted connect still
held its sandbox open and could touch it afterwards.

Driven by ``FakeSandboxService``: a real MCP server over memory streams
whose startup is parked on a gate, so the connect is provably mid-start
when it is cancelled. ``SessionHandle.closed`` is the oracle: it turns
True when the connect's sandbox session is closed.
"""
import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
import structlog

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.stdio_adapter import (
    SandboxConnectionTask,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition
from tests.unit.fake_sandbox_service import make_fake_sandbox_service
from tests.unit._shared_session_harness import (
    lose_cancels_while_connecting,
    make_gated_server_factory,
    make_manager,
    wait_until,
)
from tests.unit._user_session_harness import (
    ConnectionGate,
    acquire,
    make_store,
    make_upstream,
    start_upstream,
    stop_upstream,
)


def connection_tasks_running() -> int:
    """Background connection tasks still alive in this event loop."""
    return sum(
        1 for task in asyncio.all_tasks()
        if not task.done()
        and task.get_coro().__qualname__.endswith(  # type: ignore[union-attr]
            "_run_in_session_context",
        )
    )


async def becomes_true(predicate: Callable[[], bool], timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.mark.asyncio
async def test_a_connect_its_caller_gave_up_on_closes_its_sandbox_at_once() -> None:
    gate = asyncio.Event()  # never set: the MCP server never finishes starting
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    try:
        lone = asyncio.create_task(mgr.ensure_shared_connected(upstream))
        await wait_until(lambda: fake.session_open_count >= 1)
        handle = fake.last_session
        assert handle is not None

        lone.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lone

        assert await becomes_true(lambda: handle.closed, timeout=2.0), (
            "the sandbox of a connect nobody waits for kept running"
        )
    finally:
        gate.set()
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_stop_returns_only_after_the_aborted_connect_closed_its_sandbox() -> None:
    gate = asyncio.Event()
    fake = make_fake_sandbox_service(
        server_factory=make_gated_server_factory(gate),
    )
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    try:
        waiting = asyncio.create_task(mgr.ensure_shared_connected(upstream))
        await wait_until(lambda: fake.session_open_count >= 1)
        handle = fake.last_session
        assert handle is not None

        await asyncio.wait_for(mgr.disconnect_upstream(upstream.id), timeout=5.0)

        assert handle.closed, (
            "Stop returned while the connect it aborted still held its "
            "sandbox open"
        )
        await asyncio.gather(waiting, return_exceptions=True)
    finally:
        gate.set()
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_stop_during_sandbox_creation_lets_it_finish_then_closes_it() -> None:
    """Stop arrives while the sandbox is still being created. Cutting the
    creation short would leave a sandbox at the provider that nothing
    here knows about. The connect finishes creating it, closes it at
    once, and only then does Stop return."""
    creating = asyncio.Event()
    fake = make_fake_sandbox_service(hold_entry=creating)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    try:
        waiting = asyncio.create_task(mgr.ensure_shared_connected(upstream))
        await wait_until(lambda: fake.entries_started >= 1)

        stop = asyncio.create_task(mgr.disconnect_upstream(upstream.id))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not stop.done(), "Stop returned while the sandbox was being created"
        creating.set()
        await asyncio.wait_for(stop, timeout=5.0)

        handle = fake.last_session
        assert handle is not None and handle.closed, (
            "the sandbox created after Stop was left open"
        )
        await asyncio.gather(waiting, return_exceptions=True)
    finally:
        creating.set()
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_session_ready_as_its_caller_gives_up_is_closed() -> None:
    """The session becomes ready in the very step its caller gives up, so
    the cancel throws the result away. Nobody holds the session then, so
    the connect must close it rather than keep it serving nobody."""
    fake = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="everything2", command="ignored")
    task = SandboxConnectionTask(
        upstream, user_id="__shared__", service=fake,
        resources=SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0),
    )
    starter: asyncio.Task[object] | None = None

    def give_up_as_it_arrives(_ready: object) -> None:
        assert starter is not None
        starter.cancel()

    # Registered before ``start`` awaits the session, so it runs first,
    # in the same step as the hand-over.
    task._session_future.add_done_callback(give_up_as_it_arrives)  # pyright: ignore[reportPrivateUsage]
    starter = asyncio.create_task(task.start())

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starter, timeout=10)
    handle = fake.last_session
    assert handle is not None and handle.closed, (
        "a session nobody holds kept its sandbox open"
    )


@pytest.mark.asyncio
async def test_a_remote_connect_its_caller_gave_up_on_closes_at_once(
    tmp_path: Path,
) -> None:
    """The same for a remote (HTTP) MCP server: the connect waiting on the
    server's handshake stops as soon as its only caller gives up."""
    gate = ConnectionGate(hold={2})  # the server never answers the handshake
    server, server_task, url = await start_upstream(gate)
    upstream = make_upstream(url)
    store = await make_store(tmp_path, {"alice@co.com": "token-1"})
    mgr = UpstreamClientManager([upstream])
    try:
        lone = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: gate.opened >= 2)
        assert connection_tasks_running() == 1

        lone.cancel()
        await asyncio.gather(lone, return_exceptions=True)

        assert await becomes_true(
            lambda: connection_tasks_running() == 0, timeout=2.0,
        ), "the connection of a connect nobody waits for kept running"
    finally:
        gate.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)


def start_in_background(
    mgr: UpstreamClientManager, upstream: UpstreamDefinition,
) -> asyncio.Task[None]:
    """The dashboard's Start: a background connect the manager tracks."""
    async def start() -> None:
        await mgr.connect_upstream(upstream)

    task = asyncio.create_task(start())
    mgr.register_background_connect_task(upstream.id, task)
    return task


async def let_others_run() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_during_start_waits_for_the_start_to_let_go() -> None:
    """The admin's Start is the only caller waiting on the connect. Stop
    aborts the connect AND cancels Start; that second cancel must not cut
    the connect's release short. Stop returns only once the sandbox the
    connect was creating is closed. (Review of the follow-up fixes, F1.)"""
    creating = asyncio.Event()
    fake = make_fake_sandbox_service(hold_entry=creating)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    try:
        start = start_in_background(mgr, upstream)
        await wait_until(lambda: fake.entries_started >= 1)

        stop = asyncio.create_task(mgr.disconnect_upstream(upstream.id))
        await let_others_run()
        assert not stop.done(), (
            "Stop returned while the Start's sandbox was still being created"
        )
        creating.set()
        await asyncio.wait_for(stop, timeout=5.0)

        handle = fake.last_session
        assert handle is not None and handle.closed
        await asyncio.gather(start, return_exceptions=True)
    finally:
        creating.set()
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_restart_never_runs_two_sandbox_creations_at_once() -> None:
    """Start clicked while a Start is still creating the sandbox: the
    route stops, then starts again. The new Start must wait until the
    old connect has closed its sandbox, or two connects work on one
    upstream and one can kill the other's sandbox."""
    creating = asyncio.Event()
    fake = make_fake_sandbox_service(hold_entry=creating)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    try:
        start_in_background(mgr, upstream)
        await wait_until(lambda: fake.entries_started >= 1)

        async def restart() -> None:
            await mgr.disconnect_upstream(upstream.id)
            await start_in_background(mgr, upstream)

        restarting = asyncio.create_task(restart())
        await let_others_run()
        assert fake.entries_started == 1, (
            "a second sandbox creation began while the first was running"
        )
        creating.set()
        await asyncio.wait_for(restarting, timeout=10.0)

        state = mgr.get_state(upstream.id)
        assert state is not None and state.shared_session is not None
        assert [h.closed for h in fake.sessions] == [True, False]
    finally:
        creating.set()
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_stop_after_the_caller_hung_up_still_waits_for_the_release() -> None:
    """The only caller hung up during the sandbox creation, then the admin
    clicks Stop. Stop must still wait for the release, not cancel again."""
    creating = asyncio.Event()
    fake = make_fake_sandbox_service(hold_entry=creating)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    try:
        lone = asyncio.create_task(mgr.ensure_shared_connected(upstream))
        await wait_until(lambda: fake.entries_started >= 1)
        lone.cancel()
        await asyncio.gather(lone, return_exceptions=True)

        stop = asyncio.create_task(mgr.disconnect_upstream(upstream.id))
        await let_others_run()
        assert not stop.done(), "Stop returned before the release"
        creating.set()
        await asyncio.wait_for(stop, timeout=5.0)

        handle = fake.last_session
        assert handle is not None and handle.closed
    finally:
        creating.set()
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_stop_while_a_connect_records_its_config_leaves_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop lands while a connect reads the config it starts from. No
    session may be left that nothing records or ever closes. (Review of
    the follow-up fixes, F2.)"""
    fake = make_fake_sandbox_service()
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    reading = asyncio.Event()
    finish_reading = asyncio.Event()
    real_hash = mgr.compute_runtime_hash

    async def slow_hash(up: UpstreamDefinition) -> str:
        reading.set()
        await finish_reading.wait()
        return await real_hash(up)

    monkeypatch.setattr(mgr, "compute_runtime_hash", slow_hash)
    try:
        waiting = asyncio.create_task(mgr.ensure_shared_connected(upstream))
        await asyncio.wait_for(reading.wait(), timeout=5.0)

        await asyncio.wait_for(mgr.disconnect_upstream(upstream.id), timeout=5.0)
        finish_reading.set()
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.sleep(0.2)

        assert fake.session_open_count == 0, (
            "the connect opened a sandbox before reading its config"
        )
        assert all(h.closed for h in fake.sessions), (
            "a session nobody holds is still open"
        )
    finally:
        finish_reading.set()
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_connect_cancelled_twice_still_finishes_letting_go() -> None:
    """Cancelled, then cancelled again while it lets go (a shutdown right
    after a hang-up): the connect must not end until its sandbox is
    closed, whoever cancels it how many times."""
    creating = asyncio.Event()
    fake = make_fake_sandbox_service(hold_entry=creating)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    task = SandboxConnectionTask(
        upstream, user_id="__shared__", service=fake,
        resources=SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0),
    )
    starter = asyncio.create_task(task.start())
    await wait_until(lambda: fake.entries_started >= 1)

    starter.cancel()
    await let_others_run()
    starter.cancel()
    await let_others_run()
    assert not starter.done(), "the connect ended while its sandbox was held"
    creating.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starter, timeout=5.0)
    handle = fake.last_session
    assert handle is not None and handle.closed


@pytest.mark.asyncio
async def test_closing_a_session_never_loses_its_callers_cancel() -> None:
    """The session's teardown swallows cancels while it finishes its
    cleanup (as E2B's does). A caller cancelled while it waits for that
    close must still see its own cancel. (Review pass 2, N1.)"""
    closing = asyncio.Event()
    fake = make_fake_sandbox_service(hold_close=closing)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    task = SandboxConnectionTask(
        upstream, user_id="__shared__", service=fake,
        resources=SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0),
    )
    await asyncio.wait_for(task.start(), timeout=10)
    try:
        closer = asyncio.create_task(task.close())
        await wait_until(lambda: fake.closes_started >= 1)
        closer.cancel()
        done, _ = await asyncio.wait({closer}, timeout=2)
        assert closer in done, (
            "the caller's cancel was lost: close() is still waiting"
        )
        assert closer.cancelled()
    finally:
        closing.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_stop_during_a_heals_close_keeps_the_server_stopped() -> None:
    """A heal is closing the stalled session (its teardown swallows
    cancels) when the admin clicks Stop. The Stop must stop the heal: it
    must not go on to open a new session and bring the server back.
    (Review pass 2, N1.)"""
    closing = asyncio.Event()
    fake = make_fake_sandbox_service(hold_close=closing)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    try:
        stale = await mgr.connect_upstream(upstream)
        heal = asyncio.create_task(mgr.reconnect_shared_fresh(upstream, stale=stale))
        await wait_until(lambda: fake.closes_started >= 1)

        stop = asyncio.create_task(mgr.disconnect_upstream(upstream.id))
        await let_others_run()
        closing.set()
        await asyncio.wait_for(stop, timeout=10)
        outcome = await asyncio.gather(heal, return_exceptions=True)

        state = mgr.get_state(upstream.id)
        assert state is not None and state.state.value == "disabled", state
        assert fake.session_open_count == 1, (
            "the heal opened a new session after the Stop"
        )
        assert isinstance(outcome[0], BaseException), outcome
    finally:
        closing.set()
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_a_connect_whose_cancel_was_lost_still_respects_the_stop() -> None:
    """Even if Stop's cancel never reaches the connect, the connect must
    not go live after the Stop: it checks, once connected, whether its
    slot was aborted meanwhile. (Review pass 2, N1, second layer.)"""
    creating = asyncio.Event()
    fake = make_fake_sandbox_service(hold_entry=creating)
    upstream = make_upstream_definition(id="everything2", command="ignored")
    mgr = make_manager(upstream, fake)
    lose_cancels_while_connecting(mgr)
    try:
        with structlog.testing.capture_logs() as logs:
            waiting = asyncio.create_task(mgr.ensure_shared_connected(upstream))
            await wait_until(lambda: fake.entries_started >= 1)

            stop = asyncio.create_task(mgr.disconnect_upstream(upstream.id))
            await let_others_run()
            creating.set()
            await asyncio.wait_for(stop, timeout=10)
            await asyncio.gather(waiting, return_exceptions=True)

        state = mgr.get_state(upstream.id)
        assert state is not None and state.shared_session is None, (
            "a connect went live after the Stop"
        )
        assert all(h.closed for h in fake.sessions)
        errors = [e["event"] for e in logs if e.get("log_level") == "error"]
        assert errors == [], f"an expected Stop was logged as an error: {errors}"
    finally:
        creating.set()
        await mgr.stop_all()
