"""Exponential backoff per key: how soon something may be tried again.

For an attempt that costs something each time and keeps failing the
same way (a forced token refresh the upstream accepts, after which it
still refuses every bearer), trying on every occasion only adds cost. The
first attempt of a streak goes at once; after the n-th attempt in a row,
the next one waits ``first_delay * 2**(n-1)`` seconds, at most
``max_delay``. ``reset`` ends the streak once the thing works again. A key
quiet for twice the longest delay is forgotten, so its next streak starts
over and keys that never succeed again do not pile up.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass

# Past this many doublings the delay is the longest one whatever the
# first delay; capping the exponent keeps the arithmetic small.
_MAX_DOUBLINGS = 32


@dataclass
class _Streak:
    attempts: int
    last_at: float


class Backoff[K: Hashable]:
    def __init__(
        self,
        *,
        first_delay_seconds: float,
        max_delay_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._first_delay = first_delay_seconds
        self._max_delay = max_delay_seconds
        self._now = now
        self._streaks: dict[K, _Streak] = {}

    def delay_after(self, attempts: int) -> float:
        """How long the next attempt waits after ``attempts`` in a row."""
        doublings = min(max(attempts - 1, 0), _MAX_DOUBLINGS)
        return min(self._first_delay * 2**doublings, self._max_delay)

    def attempt(self, key: K) -> bool:
        """Whether ``key`` may be tried now. When it may, the attempt is
        counted: the caller is expected to make it."""
        now = self._now()
        self._forget_quiet(now)
        streak = self._streaks.get(key)
        if streak is None:
            self._streaks[key] = _Streak(attempts=1, last_at=now)
            return True
        if now - streak.last_at < self.delay_after(streak.attempts):
            return False
        streak.attempts += 1
        streak.last_at = now
        return True

    def reset(self, key: K) -> None:
        """The thing works again: the next attempt goes at once."""
        self._streaks.pop(key, None)

    def _forget_quiet(self, now: float) -> None:
        quiet = [
            key for key, streak in self._streaks.items()
            if now - streak.last_at >= 2 * self._max_delay
        ]
        for key in quiet:
            del self._streaks[key]
