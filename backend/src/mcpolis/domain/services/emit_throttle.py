"""At most one log line (or analytics event) per key per interval.

For signals that can repeat many times a second — a client hammering a
closed rate-limit bucket, every request failing open while Redis is
down — one line per occurrence would flood the logs, Sentry and the
analytics bill. The first occurrence of a key in an interval is
emitted; the rest are counted, and the count rides on the key's next
emission so nothing is lost from the totals.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _KeyState:
    emitted_at: float
    swallowed: int = 0


class EmitThrottle:
    def __init__(
        self,
        interval_seconds: float,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = interval_seconds
        self._now = now
        self._state: dict[str, _KeyState] = {}
        self._last_drain = now()

    def attempt(self, key: str) -> int | None:
        """Record one occurrence of ``key``.

        ``None``: stay quiet, it was counted. An int: emit now; it is the
        number of occurrences swallowed since the key's last emission.
        """
        now = self._now()
        state = self._state.get(key)
        if state is not None and now - state.emitted_at < self._interval:
            state.swallowed += 1
            return None
        swallowed = state.swallowed if state is not None else 0
        self._state[key] = _KeyState(emitted_at=now)
        return swallowed

    def forget(self, key: str) -> int:
        """Drop ``key`` (its signal ended); return its swallowed count."""
        state = self._state.pop(key, None)
        return state.swallowed if state is not None else 0

    def drain_quiet(self) -> list[tuple[str, int]]:
        """Forget keys with no emission for two intervals.

        Returns ``(key, swallowed)`` for every forgotten key, so the
        caller can flush a count still held instead of losing it, and
        drop whatever it keeps per key (a zero count has nothing to
        flush). Scans at most once per interval, so calling it on every
        emission costs O(keys) per interval, not per call.
        """
        now = self._now()
        if now - self._last_drain < self._interval:
            return []
        self._last_drain = now
        quiet = [
            key for key, state in self._state.items()
            if now - state.emitted_at >= 2 * self._interval
        ]
        return [(key, self._state.pop(key).swallowed) for key in quiet]
