"""``BackgroundTaskSet`` holds a fire-and-forget task until it ends, and
the shutdown waits for every held task before it closes the stores."""
from __future__ import annotations

import asyncio
import gc
import inspect

import pytest

from mcpolis.domain.services.background_tasks import (
    BackgroundTaskSet,
    drain_every_set,
    refusing_new_jobs,
)
from tests.unit._weak_gate import make_weak_gate

_SETTLE_SECONDS = 2.0


def make_background_task_set() -> BackgroundTaskSet:
    return BackgroundTaskSet()


@pytest.mark.parametrize("started_by", ["spawn", "hold"])
async def test_a_task_in_the_set_survives_garbage_collection(
    started_by: str,
) -> None:
    gate = make_weak_gate()
    finished = asyncio.Event()
    tasks = make_background_task_set()

    async def job() -> None:
        await gate.pass_through()
        finished.set()

    if started_by == "spawn":
        tasks.spawn(job())
    else:
        tasks.hold(asyncio.create_task(job()))

    await gate.wait_until_parked()
    assert gate.open_after_collect(), "the task was garbage-collected"
    await asyncio.wait_for(finished.wait(), _SETTLE_SECONDS)


@pytest.mark.parametrize("ending", ["returns", "raises", "is_cancelled"])
async def test_a_task_leaves_the_set_when_it_ends(ending: str) -> None:
    tasks = make_background_task_set()
    release = asyncio.Event()

    async def job() -> None:
        await release.wait()
        if ending == "raises":
            raise RuntimeError("boom")

    task = tasks.spawn(job())
    assert len(tasks) == 1

    if ending == "is_cancelled":
        task.cancel()
    else:
        release.set()
    await asyncio.wait({task}, timeout=_SETTLE_SECONDS)
    await asyncio.sleep(0)

    assert task.done()
    assert len(tasks) == 0
    if ending == "raises":
        assert isinstance(task.exception(), RuntimeError)


async def test_cancel_all_cancels_every_running_task() -> None:
    tasks = make_background_task_set()
    never = asyncio.Event()
    running = [tasks.spawn(never.wait()) for _ in range(2)]

    tasks.cancel_all()
    await asyncio.wait(running, timeout=_SETTLE_SECONDS)
    await asyncio.sleep(0)

    assert all(task.cancelled() for task in running)
    assert len(tasks) == 0


async def test_spawn_names_the_task() -> None:
    tasks = make_background_task_set()
    task = tasks.spawn(asyncio.sleep(0), name="close_orphan_shared_u1")
    await task
    assert task.get_name() == "close_orphan_shared_u1"


async def test_a_job_given_no_name_is_named_after_its_function() -> None:
    """So the shutdown's list of the jobs it had to cut says which ones
    they were, rather than ``Task-1234``."""

    async def send_sign_in_warning() -> None:
        return None

    task = make_background_task_set().spawn(send_sign_in_warning())
    await task
    assert task.get_name().endswith("send_sign_in_warning"), task.get_name()


def test_spawn_without_a_running_loop_closes_the_job() -> None:
    tasks = make_background_task_set()
    job = asyncio.sleep(0)

    with pytest.raises(RuntimeError):
        tasks.spawn(job)

    # Closed, so Python does not also warn "never awaited".
    assert inspect.getcoroutinestate(job) == inspect.CORO_CLOSED
    assert len(tasks) == 0


async def test_a_held_job_that_fails_is_still_reported_once() -> None:
    """Holding a job must not hide its failure from asyncio's report
    ("Task exception was never retrieved", which Sentry reads), nor
    report it twice."""
    loop = asyncio.get_running_loop()
    reports: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: reports.append(str(context.get("message"))),
    )
    try:
        tasks = make_background_task_set()

        async def job() -> None:
            raise RuntimeError("boom")

        tasks.spawn(job())
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()

        assert len(tasks) == 0
        assert reports == ["Task exception was never retrieved"]
    finally:
        loop.set_exception_handler(previous)


# ── The shutdown's wait for every held job ──


async def test_drain_waits_for_jobs_started_meanwhile() -> None:
    """A job that ends by starting a follow-up (a refresh that then
    audits) is not done until the follow-up is."""
    tasks = make_background_task_set()
    events: list[str] = []

    async def follow_up() -> None:
        await asyncio.sleep(0.02)
        events.append("follow-up")

    async def job() -> None:
        await asyncio.sleep(0.02)
        tasks.spawn(follow_up())

    tasks.spawn(job())
    assert await tasks.drain(timeout=_SETTLE_SECONDS) == 0
    assert events == ["follow-up"]


async def test_drain_gives_up_after_its_timeout_and_cancels_nothing() -> None:
    tasks = make_background_task_set()
    never = asyncio.Event()
    task = tasks.spawn(never.wait())

    assert await tasks.drain(timeout=0.05) == 1
    assert not task.done()
    task.cancel()


async def test_a_set_that_refuses_new_jobs_runs_none_of_them() -> None:
    tasks = make_background_task_set()
    ran: list[str] = []

    async def job() -> None:
        ran.append("job")

    tasks.refuse_new_jobs()
    task = tasks.spawn(job())
    await asyncio.wait({task}, timeout=_SETTLE_SECONDS)

    assert task.cancelled()
    assert ran == []
    assert len(tasks) == 0


async def test_no_set_starts_a_job_while_the_stores_close() -> None:
    tasks = make_background_task_set()
    ran: list[str] = []

    async def job() -> None:
        ran.append("job")

    async with refusing_new_jobs():
        refused = tasks.spawn(job())
        await asyncio.wait({refused}, timeout=_SETTLE_SECONDS)
    accepted = tasks.spawn(job())
    await accepted

    assert refused.cancelled()
    assert ran == ["job"]


async def test_the_shutdown_waits_for_every_set_then_cancels_the_rest() -> None:
    finishing, stuck = make_background_task_set(), make_background_task_set()
    done: list[str] = []

    async def short_job() -> None:
        await asyncio.sleep(0.05)
        done.append("short")

    finished = finishing.spawn(short_job())
    hung = stuck.spawn(asyncio.Event().wait())

    await drain_every_set(0.3, unwind_timeout=_SETTLE_SECONDS)

    assert finished.done() and done == ["short"]
    assert hung.cancelled()


async def test_the_shutdown_cancels_sign_in_waits_at_once() -> None:
    """A sign-in waiting for its browser cannot finish during a shutdown
    (the callback is refused while draining): it is not waited for."""
    waits = BackgroundTaskSet(cancel_at_shutdown=True)
    waiting = waits.spawn(asyncio.Event().wait())
    loop = asyncio.get_running_loop()

    started = loop.time()
    await drain_every_set(5.0, unwind_timeout=_SETTLE_SECONDS)

    assert waiting.cancelled()
    assert loop.time() - started < 1.0
