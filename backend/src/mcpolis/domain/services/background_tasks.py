"""Hold fire-and-forget asyncio tasks until they finish, and let the
shutdown wait for them.

The event loop keeps only weak references to tasks, so a task started
with ``asyncio.create_task`` and then dropped can be garbage-collected
before it finishes (see the ``asyncio.create_task`` docs). Code that
starts a background job without awaiting it starts it through a
``BackgroundTaskSet``, which keeps a strong reference until the task is
done. ``tests/unit/test_background_tasks_held.py`` flags the common
ways of dropping a task in the backend source; it cannot see a local
that is read but not kept to the end.

Every set is also known to the shutdown (``drain_every_set``): a job
still running when the app stops (an admin action, a sign-in warning, an
audit write, a sandbox create a Stop abandoned) is waited for, within a
bound, before the stores close, and none may start while they close
(``refusing_new_jobs``). Jobs used to outlive Mongo: their writes failed
(``audit.write_failed`` at every deploy) or were lost.
"""
from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

RunningTasks = Callable[[], set[asyncio.Task[object]]]

# Every set alive in this process, for the shutdown to reach them all:
# the module-level ones and those each component makes for itself.
_every_set: weakref.WeakSet[BackgroundTaskSet] = weakref.WeakSet()

# While > 0, every set refuses new jobs (``refusing_new_jobs``).
_refusing_everywhere = 0


class JobRefused(RuntimeError):
    """A job was refused because the app is shutting down: none of it
    ran. Raised by ``finish_despite_cancels`` to the action whose next
    step it was, so that action fails visibly instead of stopping on a
    cancel nobody asked for."""


def job_name(coro: Coroutine[Any, Any, object]) -> str:
    """The name a job gets when its starter gives none: the function it
    runs, so the shutdown's log of the jobs it cut says which actions
    they were rather than ``Task-1234``."""
    return getattr(coro, "__qualname__", None) or type(coro).__name__


class BackgroundTaskSet:
    """The tasks a component started and does not await, until each ends.

    ``cancel_at_shutdown``: the jobs of this set cannot finish once the
    app stops (a sign-in waiting for its browser step: the callback is
    refused while the app drains), so the shutdown cancels them at once
    instead of waiting for them.
    """

    def __init__(self, *, cancel_at_shutdown: bool = False) -> None:
        self._tasks: set[asyncio.Task[object]] = set()
        self._refusing = False
        self.cancel_at_shutdown = cancel_at_shutdown
        _every_set.add(self)

    def spawn[T](
        self, coro: Coroutine[Any, Any, T], *, name: str | None = None,
    ) -> asyncio.Task[T]:
        """Start ``coro`` as a task and hold it until it is done. The task
        is named ``name``, or after the function ``coro`` runs.

        While the set refuses new jobs, the task is cancelled before its
        first step, so none of ``coro`` runs, and it is not held."""
        try:
            task = asyncio.create_task(
                coro, name=name if name is not None else job_name(coro),
            )
        except BaseException:
            # No running loop (or a closed one): the job never starts.
            # Close it so Python does not also warn it was never awaited.
            coro.close()
            raise
        return self.hold(task)

    def hold[T](self, task: asyncio.Task[T]) -> asyncio.Task[T]:
        """Hold a task started elsewhere until it is done. While the set
        refuses new jobs, the task is cancelled instead."""
        if self.refuses_new_jobs:
            task.cancel()
            logger.warning("background_task.refused", task=task.get_name())
            return task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def refuses_new_jobs(self) -> bool:
        return self._refusing or _refusing_everywhere > 0

    def refuse_new_jobs(self) -> None:
        """From now on, cancel every job this set is given (its owner is
        gone, like an org runtime after its teardown)."""
        self._refusing = True

    def cancel_all(self) -> None:
        """Cancel every task still running. Each leaves the set once it
        has handled the cancellation."""
        for task in self.running():
            task.cancel()

    def running(self) -> set[asyncio.Task[object]]:
        """The tasks of the running event loop not done yet, except the
        caller's own task. (A task of an event loop that was closed
        before it ended, in a test process, never ends.)"""
        me = asyncio.current_task()
        loop = asyncio.get_running_loop()
        return {
            task for task in self._tasks
            if not task.done() and task is not me and task.get_loop() is loop
        }

    async def drain(self, timeout: float | None = None) -> int:
        """Wait until no task of this set runs, ``timeout`` seconds at
        most (``None``: no limit), including tasks started meanwhile.
        Returns how many still run. Cancels nothing."""
        return await _wait_until_idle(self.running, timeout)

    def __len__(self) -> int:
        return len(self._tasks)


def _running_everywhere() -> set[asyncio.Task[object]]:
    return {task for tasks in list(_every_set) for task in tasks.running()}


async def _wait_until_idle(
    running: RunningTasks, timeout: float | None,
) -> int:
    """Wait until ``running()`` is empty, ``timeout`` seconds at most.
    Jobs a finishing job starts are waited for too. Returns how many
    still run."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while True:
        pending: set[asyncio.Task[object]] = running()
        if not pending:
            return 0
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            return len(pending)
        # ``asyncio.wait`` never cancels what it waits on.
        await asyncio.wait(
            pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED,
        )


async def drain_every_set(timeout: float, *, unwind_timeout: float) -> None:
    """The shutdown's wait for every background job in the process.

    Cancels at once the jobs of the sets that cannot finish during a
    shutdown (``cancel_at_shutdown``), then waits up to ``timeout``
    seconds for every other one, including jobs those start meanwhile.
    What still runs then is cancelled and given ``unwind_timeout``
    seconds to wind down. Logs what it had to cancel."""
    for tasks in list(_every_set):
        if tasks.cancel_at_shutdown:
            tasks.cancel_all()
    waited = asyncio.get_running_loop().time()
    still_running = await _wait_until_idle(_running_everywhere, timeout)
    if still_running == 0:
        logger.info(
            "background_jobs.drained",
            duration_seconds=round(asyncio.get_running_loop().time() - waited, 3),
        )
        return
    leftovers = _running_everywhere()
    logger.warning(
        "background_jobs.cancelled_at_shutdown",
        count=len(leftovers),
        tasks=sorted(task.get_name() for task in leftovers)[:20],
        timeout_seconds=timeout,
    )
    for task in leftovers:
        task.cancel()
    await asyncio.wait(leftovers, timeout=unwind_timeout)


@contextlib.asynccontextmanager
async def refusing_new_jobs() -> AsyncIterator[None]:
    """No set starts a job inside this block: the shutdown closes the
    stores in it, and a job started then would find them closed. Lifted
    on the way out, for a process that builds another app (the tests);
    in production the process exits right after."""
    global _refusing_everywhere
    _refusing_everywhere += 1
    try:
        yield
    finally:
        _refusing_everywhere -= 1
