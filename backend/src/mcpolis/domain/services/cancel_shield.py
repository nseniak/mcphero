"""Run an admin action to its end, even when its caller is cancelled.

An admin action writes to several stores one after the other: the saved
config, the running policy, the token registry, the membership rows, the
audit log. Cut half-way, it leaves them disagreeing: a role renamed while
its service tokens still name the old role (zero tools), a removed
teammate still an admin in the running policy, a new MCP saved as started
that nobody started, no audit row for what did happen.

Two kinds of cancel can cut it:

- an anyio cancel scope. The MCP SDK cancels a tool call this way on the
  client's ``notifications/cancelled`` and when the session closes, and
  Starlette's ``BaseHTTPMiddleware`` does it to a dashboard request. anyio
  raises it again at every ``await`` until the scope exits;
- a native ``Task.cancel()``. uvicorn cancels the request tasks still
  running when its graceful-shutdown time is up. An anyio shield does not
  stop it.

``finish_despite_cancels`` runs the action in a task of its own, which
neither kind reaches. When the caller is cancelled, it waits for the
action under a shield, then passes the cancel on. Passing it on matters:
a tool call that returns normally after its MCP client cancelled it makes
the SDK answer the request a second time, which fails with "Request
already responded to" and drops the client's whole session.

Each step of an action is bounded by its own store or network timeout.
A caller that must not wait long once cancelled (an Admin MCP tool call:
its session's shutdown waits for it) passes ``wait_after_cancel``; the
action then goes on in the background, held until it ends.

The same helper carries every other piece of work a cancel must not cut
half-way, each with the options it needs: the gateway's audit write
(``tool_router._write_audit_row``), a sandbox kill
(``E2BSandboxService._finish_despite_cancels``), a connect letting go of
its transport (``ConnectionTaskBase._abandon_despite_cancels``) and a
token refresh (``upstream_connection_service._refresh_to_completion``,
``oauth_refresh._refresh_attempt``).
So a fix to how cancels are handled lands here, once.
"""
from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import anyio
import structlog

from mcpolis.domain.services.background_tasks import (
    BackgroundTaskSet,
    JobRefused,
    job_name,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Every action started here, held until it ends: also once its caller
# stopped waiting for it.
_running_actions = BackgroundTaskSet()


@dataclass(frozen=True)
class TimeLimit[T]:
    """How long the caller waits for the work at most, cancelled or not.

    Work still running then is cut: ``on_cut`` reports it (its value is
    what the caller gets, unless a cancel is passed on), then the work is
    cancelled once and handed to ``cut_work_held_by``, which holds it
    until it ends. The caller does not wait for that: cut work may hold
    on to its cancel to finish a cleanup, as an E2B call does.
    """

    seconds: float
    on_cut: Callable[[], T]
    cut_work_held_by: BackgroundTaskSet


async def finish_despite_cancels[T](
    work: Coroutine[Any, Any, T],
    *,
    name: str | None = None,
    wait_after_cancel: float | None = None,
    time_limit: TimeLimit[T] | None = None,
    held_by: BackgroundTaskSet | None = _running_actions,
    on_failure: Callable[[BaseException], T] | None = None,
    pass_cancel_on: bool = True,
) -> T:
    """Await ``work`` to its end, whatever cancels its caller meanwhile;
    then raise the cancel, if one arrived, or return ``work``'s result
    (raise its exception).

    Once the caller is cancelled it waits at most ``wait_after_cancel``
    seconds more (``None``: until ``work`` ends); ``work`` itself is never
    cancelled here, except by a ``time_limit``.

    The other options, for the work that needs them:

    - ``name``: what the logs call ``work`` (the shutdown's list of the
      jobs it had to cut, a refusal, a failure nobody reads). Defaults to
      the function ``work`` runs.
    - ``time_limit``: bounds the whole wait, cancelled or not; see
      ``TimeLimit``.
    - ``held_by``: the set that holds ``work``'s task until it ends, so
      the shutdown waits for it (``drain_every_set``), and refuses to
      start it while the stores close: ``JobRefused`` is then raised, or
      given to ``on_failure``, and none of ``work`` runs. ``None``: only
      this call holds it, as if ``work`` ran inline; it is never refused,
      and this call waits for it to end (or to be cut by ``time_limit``).
    - ``on_failure``: reports ``work``'s failure instead of raising it, or
      of logging it as ``cancelled_action.failed`` once the caller was
      cancelled. Its value is returned.
    - ``pass_cancel_on``: ``False`` drops the caller's cancel and returns
      ``work``'s outcome as if none had arrived, for a caller that is
      already handling a cancel of its own and raises that one itself.

    ``wait_after_cancel`` leaves ``work`` running once the caller stops
    waiting, so it needs ``held_by`` and ``pass_cancel_on``, and does not
    combine with ``time_limit``.
    """
    if wait_after_cancel is not None and (
        time_limit is not None or held_by is None or not pass_cancel_on
    ):
        work.close()
        raise ValueError(
            "wait_after_cancel leaves the work running: it needs held_by "
            "and pass_cancel_on, and no time_limit",
        )
    name = name if name is not None else job_name(work)
    if held_by is None:
        task = asyncio.create_task(work, name=name)
    else:
        task = held_by.spawn(work, name=name)
        if held_by.refuses_new_jobs:
            # Cancelled before its first step: a plain cancel would reach
            # the caller as if someone had cancelled it, and an action
            # stopping half-done on it would leave no trace.
            refused = JobRefused(f"{name}: refused, the app is shutting down")
            if on_failure is not None:
                return on_failure(refused)
            raise refused
    loop = asyncio.get_running_loop()
    deadline = None if time_limit is None else loop.time() + time_limit.seconds
    cancel: asyncio.CancelledError | None = None
    try:
        # A cancel of the caller interrupts this wait, never ``task``.
        await _wait_until(task, deadline)
    except asyncio.CancelledError as first_cancel:
        cancel = first_cancel
        if wait_after_cancel is not None:
            deadline = loop.time() + wait_after_cancel
        with anyio.CancelScope(shield=True):
            await _wait_ignoring_cancels(task, deadline)
    if not task.done():
        if time_limit is not None:
            outcome = time_limit.on_cut()
            task.cancel()
            time_limit.cut_work_held_by.hold(task)
            if cancel is not None and pass_cancel_on:
                raise cancel
            return outcome
        # ``wait_after_cancel`` is over: ``work`` goes on, held by
        # ``held_by``, and its failure is reported when it ends.
        assert cancel is not None
        task.add_done_callback(
            functools.partial(_report_failure, on_failure=on_failure),
        )
        raise cancel
    if cancel is not None and pass_cancel_on:
        _report_failure(task, on_failure=on_failure)
        raise cancel
    if on_failure is not None and not task.cancelled():
        failure = task.exception()
        if failure is not None:
            return on_failure(failure)
    return task.result()


def runs_to_completion[**P, T](
    action: Callable[P, Coroutine[Any, Any, T]],
) -> Callable[P, Coroutine[Any, Any, T]]:
    """Make every call of the async ``action`` run to its end once
    started, as ``finish_despite_cancels`` does."""

    @functools.wraps(action)
    async def run(*args: P.args, **kwargs: P.kwargs) -> T:
        return await finish_despite_cancels(action(*args, **kwargs))

    return run


async def _wait_until(task: asyncio.Task[object], deadline: float | None) -> None:
    """Wait for ``task`` to end, until loop time ``deadline`` at most
    (``None``: no limit). A cancel of the waiting task is raised here;
    ``asyncio.wait`` never passes it on to ``task``."""
    if task.done():
        return
    timeout = None
    if deadline is not None:
        timeout = deadline - asyncio.get_running_loop().time()
        if timeout <= 0:
            return
    await asyncio.wait({task}, timeout=timeout)


async def _wait_ignoring_cancels(
    task: asyncio.Task[object], deadline: float | None,
) -> None:
    """Wait for ``task`` to end, until loop time ``deadline`` at most
    (``None``: no limit). A further native cancel of the waiting task is
    caught: the first one is already being handled."""
    loop = asyncio.get_running_loop()
    while not task.done():
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            return
        try:
            # ``asyncio.wait`` never passes a cancel on to ``task``.
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            continue


def _report_failure(
    task: asyncio.Task[object],
    *,
    on_failure: Callable[[BaseException], object] | None,
) -> None:
    """The caller was cancelled, so nobody reads the work's outcome. A
    failure is reported with ``on_failure``, or logged here, instead of
    being lost."""
    if task.cancelled():
        return
    failure = task.exception()
    if failure is None:
        return
    if on_failure is not None:
        on_failure(failure)
        return
    logger.warning(
        "cancelled_action.failed",
        action=task.get_name(),
        error_class=type(failure).__name__,
        exc_info=failure,
    )
