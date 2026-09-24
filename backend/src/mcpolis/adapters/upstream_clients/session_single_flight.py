"""One connect at a time per session slot; callers who arrive during it
share it.

A *slot* is one place a live MCP session lives: an upstream's shared
session (keyed by upstream id), or one user's session on an upstream
(keyed by ``(user_id, upstream_id)``). Every connect for a slot runs as a
*flight*: an ``asyncio.Task`` this class owns, not the caller who started
it. That ownership is what fixes the bug class:

- A caller who needs the slot while a flight runs WAITS for it instead of
  starting its own. Two connects for one slot used to overlap, and since
  a connect begins by closing what is there, the second one closed the
  session the first had just built for its caller (Sentry
  MCPOLIS-BACKEND-W: two tool calls 70 ms apart, the first one's lookup
  raised ``KeyError``).
- The protection sits inside the functions every connect goes through,
  not at the call sites. Protection at call sites is how this broke: two
  entry points coalesced, five did not, and a new caller would not have
  known to.
- There is no lock. The lazy attach and the heal used to take an
  ``asyncio.Lock`` around their connect. A lock is not re-entrant, so a
  second one inside the shared connect would deadlock them. A flight is a
  task other callers await; nothing is ever taken twice, and a flight
  that waits on itself raises instead of hanging.

Three ways to ask, by what the caller needs:

- ``ensure``: any live session. Joins a flight in progress (checked first:
  a flight in progress is replacing the live session for a reason), else
  reuses the live session, else starts a flight.
- ``renew``: a session other than ``stale``, the one the caller saw fail.
  Joins a flight in progress, since that produces a new session. If the
  live session is already not ``stale``, someone else replaced it; use
  it rather than replacing it again under whoever moved onto it.
- ``replace``: a session built from THIS caller's inputs (a fresh
  sign-in). Waits out a flight in progress without adopting its result,
  because that flight started from older inputs, then runs its own.
  Replacements queue up in order, so the latest sign-in wins.

While a replacement is queued or running for a slot, ``ensure`` and
``renew`` wait for it to be over, then decide again. They never join a
replacement's flight, whose failure handling belongs to the sign-in that
started it, and never join the older flight a replacement is waiting
out, whose session the replacement is about to close.

Cancellation: a caller that stops waiting (its client hung up, its
timeout fired) leaves the flight running for the others. When the LAST
waiter leaves, the flight is cancelled, which is what a lone caller's
cancellation always did. ``abort`` cancels a flight whoever is waiting
(Stop). ``shut_down`` aborts every flight and refuses new ones, so a caller
that was waiting its turn cannot start a connect after the teardown. A
waiter whose flight someone else cancelled gets ``ConnectAborted``, never a
``CancelledError`` it would take for its own.
"""
from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

import structlog
from mcp.client.session import ClientSession

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

K = TypeVar("K", bound=Hashable)

OpenSession = Callable[[], Awaitable[ClientSession]]
CurrentSession = Callable[[], ClientSession | None]
LogFields = Callable[[K], dict[str, str]]


class ConnectAborted(RuntimeError):
    """The connect this caller waited on was cancelled by someone else (a
    Stop, a shutdown), so there is no session to hand back."""


@dataclass(eq=False)
class _Flight:
    task: asyncio.Task[ClientSession]
    waiters: int = 0
    # Set once the flight has been cancelled. A caller arriving after that
    # waits for it to wind down before starting a new one, so two connects
    # never run for one slot at the same time.
    abandoned: bool = False


class SessionSingleFlight(Generic[K]):
    """Per-slot flights. See the module docstring."""

    def __init__(self, label: str, log_fields: LogFields[K]) -> None:
        self._label = label
        self._log_fields = log_fields
        self._flights: dict[K, _Flight] = {}
        # Replacements queued or running, per slot, oldest first. Each
        # future resolves when that replacement is over, whichever way.
        self._replacements: dict[K, list[asyncio.Future[None]]] = {}
        # Aborts per slot so far. A replacement that was waiting its turn
        # when its slot was aborted gives up instead of starting after it.
        self._aborts: dict[K, int] = {}
        self._shut_down = False

    def in_flight(self, key: K) -> bool:
        flight = self._current(key)
        return flight is not None and not flight.abandoned

    def abort_count(self, key: K) -> int:
        """How many times ``key`` has been aborted so far. A connect reads
        it as it starts, then asks ``aborted_since`` once connected."""
        return self._aborts.get(key, 0)

    def aborted_since(self, key: K, count: int) -> bool:
        """Whether ``key`` was aborted after ``abort_count`` read ``count``.

        A connect asks this before it records the session it built. The
        abort's cancel normally stops it long before; this holds even when
        something on the way swallowed the cancel (a teardown that must
        finish its cleanup does), so an aborted slot never gets a session.
        """
        return self._aborts.get(key, 0) != count

    async def ensure(
        self,
        key: K,
        *,
        current: CurrentSession,
        open_session: OpenSession,
    ) -> ClientSession:
        """Any live session for ``key``."""
        self._refuse_reentry(key)
        while True:
            if await self._wait_out_replacements(key):
                continue
            flight = self._current(key)
            if flight is None:
                live = current()
                if live is not None:
                    return live
                return await self._join(key, self._start(key, open_session))
            if flight.abandoned:
                await _wind_down(flight.task)
                continue
            return await self._join(key, flight, joined=True)

    async def renew(
        self,
        key: K,
        *,
        current: CurrentSession,
        open_session: OpenSession,
        stale: ClientSession | None,
    ) -> ClientSession:
        """A session other than ``stale``. ``stale=None`` means the caller
        cannot say which session failed, so the live one is replaced."""
        self._refuse_reentry(key)
        while True:
            if await self._wait_out_replacements(key):
                continue
            flight = self._current(key)
            if flight is None:
                if stale is not None:
                    live = current()
                    if live is not None and live is not stale:
                        logger.info(
                            "upstream.client.connect.already_renewed",
                            slot=self._label,
                            **self._log_fields(key),
                        )
                        return live
                return await self._join(key, self._start(key, open_session))
            if flight.abandoned:
                await _wind_down(flight.task)
                continue
            return await self._join(key, flight, joined=True)

    async def replace(
        self, key: K, *, open_session: OpenSession,
    ) -> ClientSession:
        """A session built by ``open_session``, which carries this caller's
        own inputs."""
        self._refuse_reentry(key)
        aborts_seen = self._aborts.get(key, 0)
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        queue = self._replacements.setdefault(key, [])
        earlier = list(queue)
        queue.append(done)
        try:
            for previous in earlier:
                await asyncio.wait({previous})
            while (flight := self._current(key)) is not None:
                await _wind_down(flight.task)
            if self._aborts.get(key, 0) != aborts_seen:
                # A Disconnect aborted the slot while this waited. Starting
                # now would land a session right after it; this caller's
                # own credentials may be in hand, so nothing else stops it.
                raise ConnectAborted(
                    f"the {self._label} slot was aborted while this "
                    "replacement waited",
                )
            return await self._join(key, self._start(key, open_session))
        finally:
            done.set_result(None)
            queue.remove(done)
            if not queue and self._replacements.get(key) is queue:
                del self._replacements[key]

    def abort(self, key: K) -> asyncio.Task[ClientSession] | None:
        """Cancel the flight for ``key``, whoever waits on it, and return
        it so the caller can wait for it to wind down.

        A replacement queued for the slot gives up rather than start
        afterwards. A connect that asks to abort its own slot is refused:
        it would cancel itself mid-step and fail every caller waiting on
        it."""
        flight = self._current(key)
        if flight is not None and flight.task is asyncio.current_task():
            raise RuntimeError(
                f"{self._label} connect tried to abort itself",
            )
        self._aborts[key] = self._aborts.get(key, 0) + 1
        if flight is None:
            return None
        if not flight.abandoned:
            logger.info(
                "upstream.client.connect.aborted",
                slot=self._label,
                waiters=flight.waiters,
                **self._log_fields(key),
            )
            _cancel_once(flight)
        return flight.task

    def abort_matching(
        self, matches: Callable[[K], bool],
    ) -> list[asyncio.Task[ClientSession]]:
        """``abort`` every flight whose key ``matches``, for a teardown
        that covers several slots (every connect of one user). Returns the
        aborted flights so the caller can wait for them to wind down."""
        aborted: list[asyncio.Task[ClientSession]] = []
        # Slots with only a queued replacement count too: between two
        # queued sign-ins nothing runs, yet the next one must not start.
        for key in set(self._flights) | set(self._replacements):
            if matches(key):
                task = self.abort(key)
                if task is not None:
                    aborted.append(task)
        return aborted

    def shut_down(self) -> list[asyncio.Task[ClientSession]]:
        """Abort every flight and refuse any new one from now on, then
        return the aborted flights so the caller can wait for them.

        Refusing matters as much as aborting: a replacement waiting out an
        aborted flight, or a caller waiting for one to wind down, would
        otherwise start a fresh connect after the teardown and land a
        session in a manager that is gone.
        """
        self._shut_down = True
        return self.abort_matching(lambda _key: True)

    def _refuse_reentry(self, key: K) -> None:
        flight = self._flights.get(key)
        if flight is not None and flight.task is asyncio.current_task():
            raise RuntimeError(
                f"{self._label} connect waited on itself; it would never "
                "finish",
            )

    async def _wait_out_replacements(self, key: K) -> bool:
        """Wait until the latest queued replacement for ``key`` is over.
        Returns whether there was one, so the caller decides again."""
        queue = self._replacements.get(key)
        if not queue:
            return False
        await asyncio.wait({queue[-1]})
        return True

    def _current(self, key: K) -> _Flight | None:
        flight = self._flights.get(key)
        if flight is not None and flight.task.done():
            # Finished, but its done-callback has not run yet. Never join
            # a finished flight: a failure would reach a caller who should
            # have started a fresh attempt.
            del self._flights[key]
            return None
        return flight

    def _start(self, key: K, open_session: OpenSession) -> _Flight:
        if self._shut_down:
            raise ConnectAborted(f"the {self._label} connects are shut down")
        task = asyncio.create_task(
            _run(open_session), name=f"connect:{self._label}:{key!r}",
        )
        flight = _Flight(task=task)
        self._flights[key] = flight
        task.add_done_callback(functools.partial(self._forget, key, flight))
        return flight

    def _forget(
        self, key: K, flight: _Flight, task: asyncio.Task[ClientSession],
    ) -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]
        if not task.cancelled():
            # Mark the outcome as seen. When every waiter has left, nobody
            # else reads it, and asyncio would log it as never retrieved.
            task.exception()

    async def _join(
        self, key: K, flight: _Flight, *, joined: bool = False,
    ) -> ClientSession:
        if joined:
            logger.info(
                "upstream.client.connect.joined",
                slot=self._label,
                waiters=flight.waiters + 1,
                **self._log_fields(key),
            )
        flight.waiters += 1
        gave_up = False
        try:
            return await asyncio.shield(flight.task)
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if me is not None and me.cancelling() > 0:
                gave_up = True
                raise
            raise ConnectAborted(
                f"the {self._label} connect was cancelled",
            ) from None
        finally:
            flight.waiters -= 1
            if (
                gave_up
                and flight.waiters == 0
                and not flight.task.done()
                and not flight.abandoned
            ):
                logger.info(
                    "upstream.client.connect.abandoned",
                    slot=self._label,
                    **self._log_fields(key),
                )
                _cancel_once(flight)


def _cancel_once(flight: _Flight) -> None:
    """Cancel a flight, once. A connect told to stop lets go of its
    transport before it ends; a second cancel would cut that short, and
    whoever then waits for it to wind down would go on while the
    transport is still held (a Stop just after the last caller hung up,
    a Stop that also cancels the admin's Start waiting on it)."""
    flight.abandoned = True
    flight.task.cancel()


async def _run(open_session: OpenSession) -> ClientSession:
    return await open_session()


async def _wind_down(task: asyncio.Task[ClientSession]) -> None:
    """Wait for a flight to finish without adopting its outcome, and
    without cancelling it if this caller is cancelled while waiting."""
    if task is asyncio.current_task():
        raise RuntimeError("a connect waited on itself; it would never finish")
    await asyncio.wait({task})


__all__ = ["ConnectAborted", "SessionSingleFlight"]
