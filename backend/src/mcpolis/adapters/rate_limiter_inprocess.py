"""In-process ``RateLimiter`` used by standalone mode.

Stores a deque of hit timestamps per key. On every ``check`` call we
evict entries older than the window, count what's left, and either
accept + append the new timestamp or reject.

No locking — asyncio is cooperative and every code path between
``pop`` and ``append`` is synchronous, so there's no scheduling point
for another coroutine to observe a torn state.
"""
from __future__ import annotations

from collections import deque
from time import monotonic

from mcpolis.domain.ports.rate_limiter import RateLimitResult


class InProcessRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    async def check(
        self, key: str, *, limit: int, window_seconds: float,
    ) -> RateLimitResult:
        now = monotonic()
        window_start = now - window_seconds
        hits = self._hits.get(key)
        if hits is None:
            hits = deque[float]()
            self._hits[key] = hits

        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= limit:
            # Denied — do not record, otherwise persistent hammering
            # could indefinitely extend the lockout window. Report how
            # long until the oldest hit ages out.
            oldest = hits[0]
            retry_after = max(0.0, (oldest + window_seconds) - now)
            return RateLimitResult(allowed=False, retry_after=retry_after)

        hits.append(now)
        return RateLimitResult(allowed=True)

    async def close(self) -> None:
        self._hits.clear()
