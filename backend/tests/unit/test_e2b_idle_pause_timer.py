"""``IdlePauseTimer`` — re-arms E2B's pause timer on caller traffic only.

E2B pauses a sandbox a fixed time after the last ``set_timeout``,
whatever the traffic, so without this a sandbox in constant use paused
every 60 s (production, 2026-10-01). These tests pin the promises the
timer makes:

- a caller's call re-arms the timer, promptly, and none is ever lost;
- an unanswered call keeps re-arming it, up to a cap;
- nothing else re-arms it: no traffic, the program talking on its own,
  or the gateway's own housekeeping (fair use: not a keep-alive);
- refreshes never overlap, never stall the loop, and never run once the
  stream is gone.

Real time with small windows, and only lower bounds or generous upper
bounds on timing, so a loaded machine slows them down without making
them fail.
"""
from __future__ import annotations

import asyncio
import time

import pytest
import structlog

from mcpolis.adapters.sandbox_e2b.client import E2BNotFoundError, E2BSDKError
from mcpolis.adapters.sandbox_e2b.idle_pause_timer import (
    COUNTED_METHODS,
    MIN_REFRESH_GAP_SECONDS,
    IdlePauseTimer,
)


class RecordingRefresh:
    """A stand-in for ``sandbox.set_timeout`` that records each call."""

    def __init__(self) -> None:
        self.started: list[float] = []
        self.running = 0
        self.max_running = 0
        # When set, each call waits for it, to hold a refresh open.
        self.gate: asyncio.Event | None = None
        # Raised by the next calls, in order.
        self.errors: list[Exception] = []
        # Raised by every call, when set.
        self.always_raises: Exception | None = None

    async def __call__(self) -> None:
        self.started.append(time.monotonic())
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            # Yield like a real API call, so a loop that retries at once
            # spins visibly instead of freezing the event loop.
            await asyncio.sleep(0)
            if self.always_raises is not None:
                raise self.always_raises
            if self.errors:
                raise self.errors.pop(0)
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.running -= 1


def make_timer(
    *,
    idle: float,
    max_awaited: float = 300.0,
    min_gap: float = MIN_REFRESH_GAP_SECONDS,
) -> IdlePauseTimer:
    """idle 0.4 s gives a 0.1 s gap; idle 40 s gives the 5 s maximum."""
    return IdlePauseTimer(
        idle_seconds=idle,
        session_id="test-session",
        min_refresh_gap_seconds=min_gap,
        max_awaited_call_seconds=max_awaited,
    )


def make_running_timer(
    *,
    idle: float,
    max_awaited: float = 300.0,
    min_gap: float = MIN_REFRESH_GAP_SECONDS,
) -> tuple[IdlePauseTimer, RecordingRefresh, asyncio.Task[None]]:
    timer = make_timer(idle=idle, max_awaited=max_awaited, min_gap=min_gap)
    refresh = RecordingRefresh()
    task = asyncio.create_task(timer.run(refresh))
    return timer, refresh, task


def complete_call(timer: IdlePauseTimer, request_id: int) -> None:
    """A caller's tool call, sent and answered."""
    timer.request_sent(request_id, "tools/call")
    timer.response_received(request_id)


async def stop_timer(timer: IdlePauseTimer, task: asyncio.Task[None]) -> None:
    timer.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def wait_for_refreshes(
    refresh: RecordingRefresh, count: int, *, within: float,
) -> None:
    deadline = time.monotonic() + within
    while len(refresh.started) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


# ---------- caller traffic re-arms the timer ----------


@pytest.mark.asyncio
async def test_no_traffic_means_no_refresh() -> None:
    """A sandbox nobody uses must pause on schedule.

    Terms §3 fair use: unmetered stdio MCPs exclude anything that
    defeats the sandbox's auto-pause. A timer that re-armed on its own
    would be exactly that, so with no traffic it must send nothing.
    """
    timer, refresh, task = make_running_timer(idle=0.4)
    await asyncio.sleep(1.0)  # two and a half idle windows
    await stop_timer(timer, task)
    assert refresh.started == [], (
        f"no traffic must mean no refresh; got {len(refresh.started)}"
    )


@pytest.mark.parametrize("method", sorted(COUNTED_METHODS))
@pytest.mark.asyncio
async def test_every_caller_request_counts(method: str) -> None:
    """Each request a caller can cause re-arms the timer."""
    timer, refresh, task = make_running_timer(idle=40)
    timer.request_sent(1, method)
    await wait_for_refreshes(refresh, 1, within=1.0)
    await stop_timer(timer, task)
    assert len(refresh.started) == 1, f"{method} must count as caller traffic"


@pytest.mark.asyncio
async def test_first_call_refreshes_at_once() -> None:
    """The first call after a quiet spell re-arms without waiting.

    The gap (5 s here) only spaces refreshes out; it must not delay the
    first one, or a request arriving late in the window could be paused
    before its refresh went out.
    """
    timer, refresh, task = make_running_timer(idle=40)
    sent = time.monotonic()
    complete_call(timer, 1)
    await wait_for_refreshes(refresh, 1, within=1.0)
    await stop_timer(timer, task)
    assert len(refresh.started) == 1, "the first call must refresh"
    assert refresh.started[0] - sent < 1.0, (
        "the first refresh must not wait for the 5 s gap"
    )


@pytest.mark.asyncio
async def test_calls_inside_the_gap_are_folded_into_one_refresh() -> None:
    """A burst of calls costs one refresh, not one per message.

    An AI client often fires several calls in a row; refreshing on each
    would hammer E2B's API for no benefit.
    """
    timer, refresh, task = make_running_timer(idle=40)
    for request_id in range(10):
        complete_call(timer, request_id)
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    await stop_timer(timer, task)
    assert len(refresh.started) == 1, (
        f"a burst inside the 5 s gap must cost one refresh; "
        f"got {len(refresh.started)}"
    )


@pytest.mark.asyncio
async def test_a_call_after_a_refresh_gets_its_own_after_the_gap() -> None:
    """A call just after a refresh is deferred to the gap's end, not lost.

    If it were lost, the pause would count from the earlier refresh
    and could land before the configured idle time after the LAST call.
    """
    timer, refresh, task = make_running_timer(idle=0.4)  # 0.1 s gap
    complete_call(timer, 1)
    await wait_for_refreshes(refresh, 1, within=1.0)
    complete_call(timer, 2)
    await wait_for_refreshes(refresh, 2, within=1.0)
    await stop_timer(timer, task)
    assert len(refresh.started) == 2, (
        f"the later call must get its own refresh; "
        f"got {len(refresh.started)}"
    )
    assert refresh.started[1] - refresh.started[0] >= 0.09, (
        "the second refresh must wait out the gap"
    )


@pytest.mark.asyncio
async def test_a_call_during_a_refresh_gets_one_more_never_an_overlap(
) -> None:
    """A call that arrives while a refresh is in flight needs another.

    The refresh in flight may already have been processed by E2B
    before that call, so it cannot stand for it. And the next one must
    wait for the first to finish: two refreshes of one sandbox are
    never in flight together.
    """
    # A 0.1 s gap, and a 1 s refresh limit: the refresh held open below
    # outlasts the gap, so a loop that did not wait for it would start
    # another, but it stays inside the limit.
    timer, refresh, task = make_running_timer(idle=6, min_gap=0.1)
    refresh.gate = asyncio.Event()
    complete_call(timer, 1)
    await wait_for_refreshes(refresh, 1, within=1.0)
    for request_id in range(2, 6):
        complete_call(timer, request_id)
        await asyncio.sleep(0.05)
    calls_done = time.monotonic()
    assert len(refresh.started) == 1, "no second refresh while one is open"
    refresh.gate.set()
    await wait_for_refreshes(refresh, 2, within=2.0)
    await asyncio.sleep(0.3)
    await stop_timer(timer, task)
    assert refresh.max_running == 1, "refreshes must never overlap"
    assert len(refresh.started) == 2, (
        f"calls during a refresh earn exactly one more; "
        f"got {len(refresh.started)}"
    )
    assert refresh.started[1] >= calls_done, (
        "the extra refresh must start after the calls it covers"
    )


# ---------- an unanswered call keeps it armed, up to a cap ----------


@pytest.mark.asyncio
async def test_an_unanswered_request_keeps_refreshing_until_answered(
) -> None:
    """A long tool call must not be paused underneath.

    Production: ES|QL calls ran 50-57 s against a 60 s window, and a
    call still running at the pause was cut off. While a request waits
    for its answer, the timer re-arms before the deadline; once the
    answer arrives, it goes quiet again.
    """
    timer, refresh, task = make_running_timer(idle=0.6)  # every 0.2 s
    timer.request_sent(7, "tools/call")
    await asyncio.sleep(1.0)
    during_call = len(refresh.started)
    timer.response_received(7)
    await asyncio.sleep(0.4)
    after_answer = len(refresh.started)
    await asyncio.sleep(0.8)
    await stop_timer(timer, task)
    assert during_call >= 3, (
        f"a pending call must keep the timer armed; got {during_call} "
        "refreshes in 1 s against a 0.6 s window"
    )
    assert len(refresh.started) == after_answer, (
        "once the call is answered and traffic stops, refreshing must stop"
    )


@pytest.mark.asyncio
async def test_a_pending_call_refreshes_once_per_third_of_the_window(
) -> None:
    """While a call is pending, one refresh per third of the window.

    More often only multiplies E2B calls; the deadline is still two
    thirds of the window away at each refresh.
    """
    timer, refresh, task = make_running_timer(idle=3)  # 1 s, gap 0.75 s
    timer.request_sent(7, "tools/call")
    await wait_for_refreshes(refresh, 3, within=3.0)
    await stop_timer(timer, task)
    assert len(refresh.started) >= 3, f"got {len(refresh.started)}"
    intervals = [b - a for a, b in zip(refresh.started, refresh.started[1:])]
    assert min(intervals) >= 0.9, (
        f"refreshes for a pending call must be a third of the window "
        f"apart; intervals were {[round(i, 2) for i in intervals]}"
    )


@pytest.mark.asyncio
async def test_an_abandoned_request_stops_keeping_the_sandbox_awake(
) -> None:
    """A request nobody answers keeps the sandbox up only so long.

    The MCP client never tells the server it gave up on a request, so
    a lost answer would otherwise keep the sandbox running forever.
    """
    timer, refresh, task = make_running_timer(idle=0.6, max_awaited=0.5)
    timer.request_sent(7, "tools/call")  # never answered
    await asyncio.sleep(1.2)
    settled = len(refresh.started)
    await asyncio.sleep(0.6)
    await stop_timer(timer, task)
    assert 1 <= settled <= 4, f"got {settled} refreshes before the cap"
    assert len(refresh.started) == settled, (
        "past the cap, an unanswered request must stop re-arming the timer"
    )


# ---------- nothing else re-arms it ----------


@pytest.mark.asyncio
async def test_an_answer_to_nothing_is_not_traffic() -> None:
    """The program printing answers nobody asked for must not count.

    The MCP process is the customer's own program. If an answer to no
    request counted, one printed line a minute would keep its sandbox
    running for good at our cost (independent review, 2026-10-01).
    """
    timer, refresh, task = make_running_timer(idle=0.4)
    for request_id in range(5):
        timer.response_received(1000 + request_id)
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    await stop_timer(timer, task)
    assert refresh.started == [], (
        f"unsolicited answers must not re-arm; got {len(refresh.started)}"
    )


@pytest.mark.parametrize("method", [
    "ping", "tools/list", "resources/list", "resources/templates/list",
    "prompts/list",
])
@pytest.mark.asyncio
async def test_the_gateways_own_requests_are_not_traffic(method: str) -> None:
    """The gateway's housekeeping must not keep a sandbox awake.

    A server can trigger the list requests at will (each "list changed"
    notification makes the gateway re-list everything), and the gateway
    pings a slow call every 30 s. Counting either would let the program
    keep its own sandbox up, or let a hung call outlive the cap.
    """
    timer, refresh, task = make_running_timer(idle=0.4)
    timer.request_sent(1, method)
    timer.response_received(1)
    timer.request_sent(2, method)  # and one left unanswered
    await asyncio.sleep(0.8)
    await stop_timer(timer, task)
    assert refresh.started == [], (
        f"{method} must not re-arm; got {len(refresh.started)}"
    )


@pytest.mark.asyncio
async def test_an_answer_with_its_id_as_text_still_matches() -> None:
    """A server that echoes ids as text still ends its calls.

    The MCP client library accepts "7" as the answer to request 7,
    because real servers do that. The timer must too, or each such call
    stays "pending" and re-arms the timer until the cap.
    """
    timer, refresh, task = make_running_timer(idle=0.6)  # every 0.2 s
    timer.request_sent(7, "tools/call")
    timer.response_received("7")
    await asyncio.sleep(0.4)  # the traffic refresh settles
    settled = len(refresh.started)
    await asyncio.sleep(0.8)  # four pending-call periods
    await stop_timer(timer, task)
    assert len(refresh.started) == settled, (
        "an answered call must stop re-arming, whatever its id's type"
    )


# ---------- refreshes stay safe ----------


@pytest.mark.asyncio
async def test_stop_ends_the_loop_even_with_work_pending() -> None:
    """Once the stream is gone, nothing may refresh the sandbox."""
    timer, refresh, task = make_running_timer(idle=0.6)
    timer.request_sent(7, "tools/call")
    await wait_for_refreshes(refresh, 1, within=1.0)
    timer.stop()
    await asyncio.wait_for(task, timeout=1.0)
    count = len(refresh.started)
    complete_call(timer, 8)
    await asyncio.sleep(0.4)
    assert len(refresh.started) == count, "no refresh after stop"


@pytest.mark.asyncio
async def test_a_paused_sandbox_ends_the_loop() -> None:
    """E2B answers "not found" for a paused sandbox: nothing is left to keep.

    Measured against the real API: ``set_timeout`` on a paused sandbox
    raises and does not wake it. Retrying would only add load.
    """
    timer, refresh, task = make_running_timer(idle=0.4)
    refresh.errors.append(
        E2BNotFoundError("SandboxNotFoundException", "not found"),
    )
    timer.request_sent(7, "tools/call")
    await asyncio.wait_for(task, timeout=1.0)
    complete_call(timer, 8)
    await asyncio.sleep(0.3)
    assert len(refresh.started) == 1, "no retry against a paused sandbox"


@pytest.mark.asyncio
async def test_a_failed_refresh_is_retried_after_the_gap() -> None:
    """One bad answer from E2B's API must not cost the deadline."""
    timer, refresh, task = make_running_timer(idle=0.4)  # 0.1 s gap
    refresh.errors.append(E2BSDKError("ApiException", "502 Bad Gateway"))
    complete_call(timer, 1)
    await wait_for_refreshes(refresh, 2, within=1.0)
    await stop_timer(timer, task)
    assert len(refresh.started) == 2, "a failed refresh must be retried"
    assert refresh.started[1] - refresh.started[0] >= 0.09, (
        "the retry must wait out the gap"
    )


@pytest.mark.asyncio
async def test_failed_refreshes_during_a_call_are_spaced_by_the_gap(
) -> None:
    """An E2B outage during a long call costs one retry per gap, no more.

    Without the gap, every failure would retry at once, and every busy
    sandbox would hammer an API that is already in trouble.
    """
    timer, refresh, task = make_running_timer(idle=0.6)  # 0.15 s gap
    refresh.always_raises = E2BSDKError("ApiException", "503 Unavailable")
    timer.request_sent(7, "tools/call")
    await asyncio.sleep(0.9)
    await stop_timer(timer, task)
    assert 3 <= len(refresh.started) <= 12, (
        f"0.9 s of failures must cost about one retry per 0.15 s gap; "
        f"got {len(refresh.started)} attempts"
    )


@pytest.mark.asyncio
async def test_a_stalled_refresh_is_abandoned_and_retried() -> None:
    """A refresh E2B never answers must not hold the loop.

    The E2B SDK waits up to 60 s for an answer, the whole default
    window. If the loop waited that long, the sandbox would pause under
    a live call, the very bug this timer fixes.
    """
    timer, refresh, task = make_running_timer(idle=0.6)  # 0.1 s limit
    refresh.gate = asyncio.Event()  # E2B never answers
    timer.request_sent(7, "tools/call")
    await asyncio.sleep(1.0)
    await stop_timer(timer, task)
    assert len(refresh.started) >= 3, (
        f"a stalled refresh must be abandoned and retried; "
        f"got {len(refresh.started)} attempts in 1 s"
    )
    assert refresh.max_running == 1, "an abandoned refresh must end first"


@pytest.mark.asyncio
async def test_an_unexpected_error_is_reported_once_and_ends_the_loop(
) -> None:
    """A bug must surface in the logs, not kill the timer silently."""
    timer, refresh, task = make_running_timer(idle=0.4)
    refresh.always_raises = RuntimeError("a bug, not an E2B answer")
    with structlog.testing.capture_logs() as logs:
        complete_call(timer, 1)
        await asyncio.wait_for(task, timeout=1.0)
    crashes = [
        e for e in logs if e.get("event") == "sandbox.e2b.pause_timer.crashed"
    ]
    assert [e["log_level"] for e in crashes] == ["error"]
    assert len(refresh.started) == 1, "a bug must not be retried in a loop"


class BrokenScheduleTimer(IdlePauseTimer):
    """A timer whose scheduling itself fails, outside any refresh."""

    def _next_refresh_at(self, now: float) -> float | None:
        raise RuntimeError("a bug in the schedule")


@pytest.mark.asyncio
async def test_an_error_outside_the_refresh_is_reported_too() -> None:
    timer = BrokenScheduleTimer(idle_seconds=0.4, session_id="test-session")
    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(timer.run(RecordingRefresh()), timeout=1.0)
    crashes = [
        e for e in logs if e.get("event") == "sandbox.e2b.pause_timer.crashed"
    ]
    assert [e["log_level"] for e in crashes] == ["error"]
