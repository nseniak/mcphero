"""Keeps E2B's pause timer counting MCP idle time, not time since connect.

E2B pauses a sandbox ``timeout`` seconds after it was created or after
the last ``set_timeout`` call. Traffic does not move that deadline:
measured 2026-10-01, a sandbox created with a 20 s timeout paused 20.1 s
after creation while a line went through its process every 4 s. The
service used to call ``set_timeout`` only when it opened a session, so
``MCPOLIS_E2B_IDLE_PAUSE_SECONDS`` (60 s) really meant "pause 60 s after
connect". In production a user calling tools the whole time got a pause
every minute: each one cost the next call a 3-6 s wake and cut off any
call still running.

So the session reports its MCP traffic here, and this re-arms the timer
for CALLER traffic only:

- a caller's request written to the MCP process (``COUNTED_METHODS``),
  or the answer to one, re-arms it at once, or at the end of the
  current refresh gap;
- while such a request still waits for its answer, it is re-armed well
  before the deadline, so a long call is never paused underneath;
- nothing else counts, and with no caller traffic the sandbox pauses
  ``idle`` seconds after the last of it, as configured.

That last point is the fair-use line (Terms §3): this must never become
a keep-alive. The MCP process is the customer's own program, so nothing
it does on its own may count: not its notifications, not an answer to a
request nobody sent, and not the requests the gateway sends because of
it (the list refreshes a ``list_changed`` notification triggers). The
gateway's liveness pings do not count either, so a call that never
answers stops keeping the sandbox awake after ``MAX_AWAITED_CALL_SECONDS``.

One loop per session, so two refreshes of a sandbox never overlap, and
the session's I/O only flips in-memory state here: no tool call ever
waits on a refresh.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from mcpolis.adapters.sandbox_e2b.client import E2BNotFoundError, E2BSDKError

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Fewest seconds between two refreshes of one sandbox's timer. Traffic
# inside the gap is folded into one refresh at its end, so the pause
# lands between ``idle`` and ``idle + gap`` seconds after the last
# traffic. Shrunk for short idle windows so the gap can never use up
# the deadline.
MIN_REFRESH_GAP_SECONDS = 5.0

# Longest a refresh may take before it is abandoned and retried. The
# E2B SDK's own request limit is 60 s, the whole default window, so one
# stalled call would otherwise let the sandbox pause under a live call.
MAX_REFRESH_SECONDS = 10.0

# How long an unanswered request keeps the sandbox awake. The MCP client
# never sends ``notifications/cancelled`` for a request it gives up on,
# and the gateway keeps waiting for as long as the server answers its
# pings, so without a cap one lost answer would keep a sandbox up for
# good. Production's longest tool call up to 2026-10-01 took 98.7 s
# (1,086 calls, none over 300 s), so 300 s leaves three times that.
MAX_AWAITED_CALL_SECONDS = 300.0

# The requests a CALLER causes. Everything else the gateway sends is its
# own housekeeping (ping, and the list requests a server's
# ``list_changed`` notification triggers), which the customer's program
# could provoke at will, so it must not keep the sandbox awake. An
# allow-list fails safe: a new method costs at worst an early pause.
COUNTED_METHODS: frozenset[str] = frozenset({
    "initialize",
    "tools/call",
    "resources/read",
    "prompts/get",
    "completion/complete",
})


def _normalize_request_id(request_id: int | str) -> int | str:
    """The key a response's id is matched under.

    Mirrors the MCP client library (``BaseSession._normalize_request_id``),
    which accepts the text ``"5"`` as the answer to request 5 because real
    servers echo ids that way. Matching raw ids instead would leave such a
    request "unanswered" and keep the sandbox awake until the cap.
    """
    if isinstance(request_id, str):
        try:
            return int(request_id)
        except ValueError:
            return request_id
    return request_id


class IdlePauseTimer:
    """Re-arms one sandbox's E2B pause timer on caller traffic.

    The session calls :meth:`request_sent` and :meth:`response_received`
    from its I/O path, runs :meth:`run` as a task once the sandbox
    exists, calls :meth:`stop` when the stream dies, and cancels the task
    at teardown.
    """

    def __init__(
        self,
        *,
        idle_seconds: float,
        session_id: str,
        min_refresh_gap_seconds: float = MIN_REFRESH_GAP_SECONDS,
        max_refresh_seconds: float = MAX_REFRESH_SECONDS,
        max_awaited_call_seconds: float = MAX_AWAITED_CALL_SECONDS,
    ) -> None:
        self._session_id = session_id
        self._gap = min(min_refresh_gap_seconds, idle_seconds / 4)
        # While a call waits for its answer, re-arm with two thirds of
        # the window still left: room for several retries if E2B's API
        # has a bad moment.
        self._awaited_refresh_every = idle_seconds / 3
        # Short enough that a stalled refresh leaves time for retries.
        self._refresh_limit = min(max_refresh_seconds, idle_seconds / 6)
        self._max_awaited = max_awaited_call_seconds
        # The sandbox's timer is armed while the session opens, after
        # this object is built. Counting from now can only make a
        # refresh early, never late.
        self._armed_at = time.monotonic()
        # No attempt yet, so the session's first traffic refreshes at
        # once. That also covers an open path that spent part of the
        # window (a docker daemon start) or did not arm it at all.
        self._last_attempt = float("-inf")
        # Traffic seen since the last refresh attempt BEGAN. Cleared
        # before the call, not after, so traffic during a refresh earns
        # its own refresh instead of being swallowed by the current one.
        self._traffic_seen = False
        # Caller requests not answered yet: normalized id -> when written.
        self._awaited: dict[int | str, float] = {}
        self._wake = asyncio.Event()
        self._stopped = False

    def request_sent(self, request_id: int | str, method: str) -> None:
        """A request is being written to the MCP process."""
        if method not in COUNTED_METHODS:
            return
        self._awaited[_normalize_request_id(request_id)] = time.monotonic()
        self._note_traffic()

    def response_received(self, request_id: int | str) -> None:
        """A response or error was read from the MCP process.

        Counts only when it answers a counted request still waiting. Any
        other answer comes from the program alone, and counting it would
        let the program keep its own sandbox awake.
        """
        if self._awaited.pop(_normalize_request_id(request_id), None) is None:
            return
        self._note_traffic()

    def stop(self) -> None:
        """The stream is gone (the sandbox paused, or the session is
        ending). No refresh can help any more, so :meth:`run` returns."""
        self._stopped = True
        self._wake.set()

    def _note_traffic(self) -> None:
        self._traffic_seen = True
        self._wake.set()

    def _still_awaiting_a_call(self, now: float) -> bool:
        """Whether an unanswered request still keeps the sandbox awake.

        Forgets requests older than the cap, so abandoned ones neither
        keep the sandbox up nor pile up here.
        """
        expired = [
            request_id
            for request_id, sent_at in self._awaited.items()
            if now - sent_at >= self._max_awaited
        ]
        for request_id in expired:
            del self._awaited[request_id]
        return bool(self._awaited)

    def _next_refresh_at(self, now: float) -> float | None:
        """When the timer next needs re-arming, or ``None`` if it never
        does without new traffic."""
        due: list[float] = []
        if self._traffic_seen:
            due.append(self._last_attempt + self._gap)
        if self._still_awaiting_a_call(now):
            due.append(max(
                self._armed_at + self._awaited_refresh_every,
                self._last_attempt + self._gap,
            ))
        return min(due) if due else None

    async def run(self, refresh: Callable[[], Awaitable[None]]) -> None:
        """Re-arm the timer through *refresh* whenever caller traffic
        calls for it, until :meth:`stop`."""
        try:
            await self._loop(refresh)
        except Exception:
            # A bug, not an E2B answer: report it once and stop. The
            # session keeps working; its sandbox just pauses ``idle``
            # seconds after the last refresh, as before this existed.
            logger.exception(
                "sandbox.e2b.pause_timer.crashed",
                session_id=self._session_id,
            )

    async def _loop(self, refresh: Callable[[], Awaitable[None]]) -> None:
        while not self._stopped:
            # Clear before reading the state: there is no await between
            # here and the wait below, so any traffic either shows in the
            # state now or sets the event the wait sees.
            self._wake.clear()
            now = time.monotonic()
            due = self._next_refresh_at(now)
            if due is None or due > now:
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        None if due is None else due - now,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            reason = "traffic" if self._traffic_seen else "awaited_call"
            self._traffic_seen = False
            self._last_attempt = now
            try:
                await asyncio.wait_for(refresh(), self._refresh_limit)
            except E2BNotFoundError:
                # E2B answers "not found" for a paused or deleted sandbox,
                # and the call does not wake it (measured 2026-10-01).
                # The stream is ending too; nothing is left to keep.
                logger.info(
                    "sandbox.e2b.pause_timer.sandbox_gone",
                    session_id=self._session_id,
                )
                return
            except (
                E2BSDKError, ConnectionError, OSError, asyncio.TimeoutError,
            ) as exc:
                # Try again after the gap; the deadline is still ahead.
                self._traffic_seen = True
                logger.warning(
                    "sandbox.e2b.pause_timer.refresh_failed",
                    session_id=self._session_id,
                    error=str(exc) or type(exc).__name__,
                )
            else:
                self._armed_at = now
                # DEBUG: the E2B SDK already logs each call at INFO.
                logger.debug(
                    "sandbox.e2b.pause_timer.refreshed",
                    session_id=self._session_id,
                    reason=reason,
                    awaited_calls=len(self._awaited),
                    duration_ms=round((time.monotonic() - now) * 1000, 1),
                )


__all__ = [
    "COUNTED_METHODS",
    "IdlePauseTimer",
    "MAX_AWAITED_CALL_SECONDS",
    "MAX_REFRESH_SECONDS",
    "MIN_REFRESH_GAP_SECONDS",
]
