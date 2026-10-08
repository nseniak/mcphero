"""In-process ``RateLimiter`` used by standalone mode.

Stores a deque of hit timestamps per key. On every ``check`` call we
evict entries older than each bucket's window, count what's left, and
either accept + append the new timestamp to every bucket or reject.

No locking — asyncio is cooperative and every code path between the
first eviction and the last append is synchronous, so there's no
scheduling point for another coroutine to observe a torn state.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from time import monotonic

from mcpolis.domain.ports.rate_limiter import RateLimitBucket, RateLimitResult

# Every this-many checks, drop keys whose hits have all aged out.
# Without it the dict keeps one entry per key ever seen (one per client
# IP for the sign-in limit), which a caller rotating IPs could grow
# without bound.
_SWEEP_EVERY_CHECKS = 1_000


class InProcessRateLimiter:
    def __init__(self, *, now: Callable[[], float] = monotonic) -> None:
        # key -> (hit timestamps, window of the bucket that last used it)
        self._hits: dict[str, tuple[deque[float], float]] = {}
        # Injectable clock. Tests pass a controllable clock so the
        # sliding-window assertions don't depend on wall-clock elapsed
        # time between calls (which flakes under CPU starvation). Prod
        # uses ``monotonic``.
        self._now = now
        self._checks_since_sweep = 0

    async def check(self, *buckets: RateLimitBucket) -> RateLimitResult:
        now = self._now()
        self._maybe_sweep(now)
        windows: list[deque[float]] = []
        refusal: RateLimitResult | None = None
        for bucket in buckets:
            hits = self._evicted_hits(bucket, now)
            windows.append(hits)
            if len(hits) < bucket.limit:
                continue
            # Full. Report how long until its oldest hit ages out; keep
            # the longest wait when several buckets are full.
            retry_after = min(
                bucket.window_seconds,
                max(0.0, (hits[0] + bucket.window_seconds) - now),
            )
            if refusal is None or retry_after > (refusal.retry_after or 0.0):
                refusal = RateLimitResult(
                    allowed=False, retry_after=retry_after, exceeded=bucket,
                )
        if refusal is not None:
            # Denied — record nothing, in any bucket.
            return refusal
        for hits in windows:
            hits.append(now)
        return RateLimitResult(allowed=True)

    def _evicted_hits(self, bucket: RateLimitBucket, now: float) -> deque[float]:
        entry = self._hits.get(bucket.key)
        if entry is None:
            hits = deque[float]()
        else:
            hits = entry[0]
        self._hits[bucket.key] = (hits, bucket.window_seconds)
        window_start = now - bucket.window_seconds
        while hits and hits[0] <= window_start:
            hits.popleft()
        return hits

    def _maybe_sweep(self, now: float) -> None:
        self._checks_since_sweep += 1
        if self._checks_since_sweep < _SWEEP_EVERY_CHECKS:
            return
        self._checks_since_sweep = 0
        stale = [
            key for key, (hits, window) in self._hits.items()
            if not hits or hits[-1] <= now - window
        ]
        for key in stale:
            del self._hits[key]

    async def close(self) -> None:
        self._hits.clear()
