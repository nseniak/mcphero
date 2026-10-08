"""``finish_despite_cancels``: an action runs to its end whatever cancels
its caller, then the caller gets the cancel.

Two kinds of cancel reach an admin action: an anyio cancel scope (the MCP
SDK on a client's ``notifications/cancelled``, Starlette's
``BaseHTTPMiddleware``) and a native ``Task.cancel()`` (uvicorn at the end
of its graceful shutdown), which an anyio shield does not stop.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable

import anyio
import pytest
import structlog

from mcpolis.domain.services.background_tasks import (
    BackgroundTaskSet,
    JobRefused,
    drain_every_set,
    refusing_new_jobs,
)
from mcpolis.domain.services.cancel_shield import (
    TimeLimit,
    finish_despite_cancels,
)
from tests.unit.factories import Gate, cancel_natively_while_gated


class HeldAction:
    """An action of two steps, held at ``gate`` between them."""

    def __init__(self, *, fails: bool = False) -> None:
        self.gate = Gate()
        self.steps: list[str] = []
        self.fails = fails

    async def run(self) -> str:
        self.steps.append("first write")
        await self.gate.hold()
        self.steps.append("second write")
        if self.fails:
            raise RuntimeError("the second write failed")
        return "done"


async def wait_until(condition_met: asyncio.Event) -> None:
    await asyncio.wait_for(condition_met.wait(), timeout=5)


async def test_an_anyio_cancel_lets_the_action_end_then_reaches_the_caller() -> None:
    action = HeldAction()
    scope = anyio.CancelScope()
    after_the_action: list[str] = []

    async def caller() -> None:
        with scope:
            await finish_despite_cancels(action.run())
            after_the_action.append("went on as if not cancelled")

    task = asyncio.create_task(caller())
    await wait_until(action.gate.reached)
    scope.cancel()
    await asyncio.sleep(0.05)
    action.gate.release.set()
    await task

    assert action.steps == ["first write", "second write"]
    assert scope.cancelled_caught
    assert after_the_action == []


async def test_a_native_cancel_lets_the_action_end_then_reaches_the_caller() -> None:
    action = HeldAction()
    task = asyncio.create_task(finish_despite_cancels(action.run()))
    await wait_until(action.gate.reached)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()  # a second one, while the action is still held
    await asyncio.sleep(0.05)
    action.gate.release.set()
    await asyncio.wait({task}, timeout=5)

    assert action.steps == ["first write", "second write"]
    assert task.cancelled()


async def test_without_a_cancel_the_result_and_the_failure_come_back() -> None:
    succeeding = HeldAction()
    succeeding.gate.release.set()
    failing = HeldAction(fails=True)
    failing.gate.release.set()

    assert await finish_despite_cancels(succeeding.run()) == "done"
    with pytest.raises(RuntimeError, match="the second write failed"):
        await finish_despite_cancels(failing.run())


async def test_a_caller_that_stops_waiting_leaves_the_action_running() -> None:
    """``wait_after_cancel`` bounds the caller's wait once cancelled; the
    action is not cancelled with it."""
    action = HeldAction()
    task = asyncio.create_task(
        finish_despite_cancels(action.run(), wait_after_cancel=0.05),
    )
    await wait_until(action.gate.reached)
    task.cancel()
    await asyncio.wait({task}, timeout=5)
    assert task.cancelled()
    assert action.steps == ["first write"]

    action.gate.release.set()
    async with asyncio.timeout(5):
        while len(action.steps) < 2:
            await asyncio.sleep(0.01)

    assert action.steps == ["first write", "second write"]


async def test_a_failure_nobody_reads_is_logged() -> None:
    action = HeldAction(fails=True)
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(finish_despite_cancels(action.run()))
        await wait_until(action.gate.reached)
        task.cancel()
        await asyncio.sleep(0.05)
        action.gate.release.set()
        await asyncio.wait({task}, timeout=5)

    assert task.cancelled()
    failures = [line for line in logs if line["event"] == "cancelled_action.failed"]
    assert [line["error_class"] for line in failures] == ["RuntimeError"]


async def test_a_caller_that_stops_waiting_at_once_leaves_the_work_running() -> None:
    """``wait_after_cancel=0``, as the token refresh uses it: the cancel
    reaches the caller at once, the work runs on, and a failure nobody
    reads is still logged when the work ends."""
    action = HeldAction(fails=True)
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(
            finish_despite_cancels(action.run(), wait_after_cancel=0),
        )
        await wait_until(action.gate.reached)
        task.cancel()
        await asyncio.wait({task}, timeout=5)
        assert task.cancelled()
        assert action.steps == ["first write"]

        action.gate.release.set()
        async with asyncio.timeout(5):
            while not any(
                line["event"] == "cancelled_action.failed" for line in logs
            ):
                await asyncio.sleep(0.01)

    assert action.steps == ["first write", "second write"]


async def test_work_held_by_a_given_set_stays_in_it_until_it_ends() -> None:
    action = HeldAction()
    tasks = BackgroundTaskSet()
    caller = asyncio.create_task(
        finish_despite_cancels(action.run(), held_by=tasks),
    )
    await wait_until(action.gate.reached)
    assert len(tasks) == 1

    action.gate.release.set()

    assert await caller == "done"
    assert len(tasks) == 0


async def test_work_held_by_its_caller_only_runs_even_while_sets_refuse_jobs() -> None:
    """While the shutdown closes the stores, every set refuses new jobs.
    Work held by its caller only (``held_by=None``) runs anyway, as work
    done inline would."""
    refused = HeldAction()
    refused.gate.release.set()
    unheld = HeldAction()
    unheld.gate.release.set()

    async with refusing_new_jobs():
        with pytest.raises(JobRefused):
            await finish_despite_cancels(refused.run())
        outcome = await finish_despite_cancels(unheld.run(), held_by=None)

    assert refused.steps == []
    assert outcome == "done"
    assert unheld.steps == ["first write", "second write"]


async def test_a_dropped_cancel_lets_the_caller_go_on_with_the_outcome() -> None:
    """``pass_cancel_on=False``, for a caller already handling a cancel of
    its own: a later cancel neither cuts the work nor reaches the caller,
    which gets the work's result."""
    action = HeldAction()
    outcomes: list[str] = []

    async def caller() -> None:
        outcomes.append(
            await finish_despite_cancels(action.run(), pass_cancel_on=False),
        )

    await cancel_natively_while_gated(action.gate, caller)

    assert action.steps == ["first write", "second write"]
    assert outcomes == ["done"]


async def test_a_dropped_cancel_still_raises_the_failure_of_the_work() -> None:
    action = HeldAction(fails=True)

    with pytest.raises(RuntimeError, match="the second write failed"):
        await cancel_natively_while_gated(
            action.gate,
            lambda: finish_despite_cancels(action.run(), pass_cancel_on=False),
        )


def make_cut_report(cuts: list[str]) -> Callable[[], str]:
    """An ``on_cut`` that records each cut and gives the caller "cut"."""

    def report_cut() -> str:
        cuts.append("cut")
        return "cut"

    return report_cut


async def hold_until_cancelled(gate: Gate, cancels: list[str]) -> str:
    """Work held at ``gate`` that records it when it is cancelled."""
    try:
        await gate.hold()
    except asyncio.CancelledError:
        cancels.append("cancelled")
        raise
    return "released"


async def held_work_ends(tasks: BackgroundTaskSet) -> None:
    async with asyncio.timeout(5):
        while len(tasks):
            await asyncio.sleep(0.01)


async def test_work_past_its_time_limit_is_cut_and_handed_over() -> None:
    """``time_limit`` bounds the wait with no cancel at all. Work still
    running then is reported, cancelled and held by ``cut_work_held_by``,
    and the caller goes on with what ``on_cut`` gave."""
    cuts: list[str] = []
    cancels: list[str] = []
    cut_work = BackgroundTaskSet()
    loop = asyncio.get_running_loop()
    started = loop.time()

    outcome = await finish_despite_cancels(
        hold_until_cancelled(Gate(), cancels),
        held_by=None,
        time_limit=TimeLimit(
            0.05, on_cut=make_cut_report(cuts), cut_work_held_by=cut_work,
        ),
    )

    assert outcome == "cut"
    assert cuts == ["cut"]
    assert loop.time() - started < 1
    assert len(cut_work) == 1, "the cut work must be held while it unwinds"
    await held_work_ends(cut_work)
    assert cancels == ["cancelled"]


async def test_a_time_limit_also_bounds_the_wait_of_a_cancelled_caller() -> None:
    """Cancelled while it waits, the caller still waits for the work up to
    the time limit only; then the work is cut and the cancel reaches the
    caller."""
    gate = Gate()  # never opened: the work never ends by itself
    cuts: list[str] = []
    cancels: list[str] = []
    cut_work = BackgroundTaskSet()
    caller = asyncio.create_task(finish_despite_cancels(
        hold_until_cancelled(gate, cancels),
        held_by=None,
        time_limit=TimeLimit(
            0.3, on_cut=make_cut_report(cuts), cut_work_held_by=cut_work,
        ),
    ))
    await wait_until(gate.reached)

    caller.cancel()
    await asyncio.sleep(0.05)
    assert not caller.done(), "the caller stopped waiting before the time limit"
    await asyncio.wait({caller}, timeout=5)

    assert caller.cancelled()
    assert cuts == ["cut"]
    await held_work_ends(cut_work)
    assert cancels == ["cancelled"]


async def test_a_time_limit_passes_an_anyio_cancel_on_after_the_cut() -> None:
    gate = Gate()  # never opened
    cuts: list[str] = []
    scope = anyio.CancelScope()
    after_the_work: list[str] = []

    async def caller() -> None:
        with scope:
            await finish_despite_cancels(
                hold_until_cancelled(gate, []),
                held_by=None,
                time_limit=TimeLimit(
                    0.1,
                    on_cut=make_cut_report(cuts),
                    cut_work_held_by=BackgroundTaskSet(),
                ),
            )
            after_the_work.append("went on as if not cancelled")

    task = asyncio.create_task(caller())
    await wait_until(gate.reached)
    scope.cancel()
    await asyncio.wait_for(task, timeout=5)

    assert cuts == ["cut"]
    assert scope.cancelled_caught
    assert after_the_work == []


async def test_work_done_within_its_time_limit_gives_its_result() -> None:
    action = HeldAction()
    action.gate.release.set()
    cuts: list[str] = []

    outcome = await finish_despite_cancels(
        action.run(),
        held_by=None,
        time_limit=TimeLimit(
            5, on_cut=make_cut_report(cuts), cut_work_held_by=BackgroundTaskSet(),
        ),
    )

    assert outcome == "done"
    assert cuts == []


def make_failure_report(failures: list[str]) -> Callable[[BaseException], str]:
    """An ``on_failure`` that records each failure's class and gives the
    caller "reported"."""

    def report_failure(failure: BaseException) -> str:
        failures.append(type(failure).__name__)
        return "reported"

    return report_failure


async def test_a_failure_given_to_on_failure_is_returned_not_raised() -> None:
    action = HeldAction(fails=True)
    action.gate.release.set()
    failures: list[str] = []

    outcome = await finish_despite_cancels(
        action.run(), on_failure=make_failure_report(failures),
    )

    assert outcome == "reported"
    assert failures == ["RuntimeError"]


async def test_on_failure_also_reports_a_failure_nobody_reads() -> None:
    """Once the caller was cancelled, the failure goes to ``on_failure``
    instead of the generic ``cancelled_action.failed`` line."""
    action = HeldAction(fails=True)
    failures: list[str] = []
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(finish_despite_cancels(
            action.run(), on_failure=make_failure_report(failures),
        ))
        await wait_until(action.gate.reached)
        task.cancel()
        await asyncio.sleep(0.05)
        action.gate.release.set()
        await asyncio.wait({task}, timeout=5)

    assert task.cancelled()
    assert failures == ["RuntimeError"]
    assert [line for line in logs if line["event"] == "cancelled_action.failed"] == []


async def test_wait_after_cancel_refuses_the_options_it_cannot_honour() -> None:
    """``wait_after_cancel`` leaves the work running once the caller stops
    waiting, so a set must hold it and the cancel must be passed on; and
    it cannot share the wait with a time limit. The work never starts."""
    unheld = HeldAction()
    dropping = HeldAction()
    limited = HeldAction()
    works = [unheld.run(), dropping.run(), limited.run()]

    with pytest.raises(ValueError):
        await finish_despite_cancels(works[0], wait_after_cancel=1, held_by=None)
    with pytest.raises(ValueError):
        await finish_despite_cancels(
            works[1], wait_after_cancel=1, pass_cancel_on=False,
        )
    with pytest.raises(ValueError):
        await finish_despite_cancels(
            works[2],
            wait_after_cancel=1,
            time_limit=TimeLimit(
                1, on_cut=lambda: "cut", cut_work_held_by=BackgroundTaskSet(),
            ),
        )

    assert [inspect.getcoroutinestate(work) for work in works] == [
        inspect.CORO_CLOSED,
    ] * 3
    assert unheld.steps == dropping.steps == limited.steps == []


async def remove_teammate_half_way(gate: Gate) -> None:
    await gate.hold()


async def test_an_action_the_shutdown_cuts_is_named_in_its_log() -> None:
    """``background_jobs.cancelled_at_shutdown`` lists the jobs the
    shutdown had to cut; it used to list them as ``Task-1234``."""
    gate = Gate()
    caller = asyncio.create_task(
        finish_despite_cancels(remove_teammate_half_way(gate)),
    )
    await wait_until(gate.reached)

    with structlog.testing.capture_logs() as logs:
        await drain_every_set(0.1, unwind_timeout=0.5)
    await asyncio.gather(caller, return_exceptions=True)

    cut = [
        line for line in logs
        if line["event"] == "background_jobs.cancelled_at_shutdown"
    ]
    assert len(cut) == 1, logs
    assert any("remove_teammate_half_way" in name for name in cut[0]["tasks"]), cut


async def test_a_step_refused_at_shutdown_fails_its_action_by_name() -> None:
    """An action still running when the shutdown starts refusing jobs (it
    outlived the job drain) and starts its next shielded step: the step
    is refused, and the action stops half-done. It used to stop on a
    plain cancel, so it ended "cancelled" and nothing said it had been
    cut half-way."""
    gate = Gate()
    steps: list[str] = []

    async def second_step() -> None:
        steps.append("second step")

    async def remove_teammate() -> None:
        steps.append("first step")
        await gate.hold()  # still running when the stores start closing
        await finish_despite_cancels(second_step())
        steps.append("done")

    with structlog.testing.capture_logs() as logs:
        caller = asyncio.create_task(finish_despite_cancels(remove_teammate()))
        await wait_until(gate.reached)
        caller.cancel()  # the request is long gone
        await asyncio.sleep(0.05)
        async with refusing_new_jobs():
            gate.release.set()
            await asyncio.wait({caller}, timeout=5)

    assert steps == ["first step"]
    failed = [line for line in logs if line["event"] == "cancelled_action.failed"]
    assert len(failed) == 1, logs
    assert failed[0]["error_class"] == "JobRefused"
    assert failed[0]["action"].endswith("remove_teammate"), failed[0]


async def test_a_refused_step_goes_to_on_failure() -> None:
    action = HeldAction()
    failures: list[str] = []

    async with refusing_new_jobs():
        outcome = await finish_despite_cancels(
            action.run(), on_failure=make_failure_report(failures),
        )

    assert outcome == "reported"
    assert failures == ["JobRefused"]
    assert action.steps == []
