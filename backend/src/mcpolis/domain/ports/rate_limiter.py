"""RateLimiter port — sliding-window rate limiting primitive.

Implementations:
- ``InProcessRateLimiter`` (standalone mode): dict of per-key timestamp
  deques. Zero dependencies, single-process semantics.
- ``RedisRateLimiter`` (cloud mode): sliding window via ZSET (ZADD +
  ZREMRANGEBYSCORE + ZCARD). Shares state across every backend hitting
  the same Redis.

Both follow the exact same contract so the FastAPI middleware doesn't
care which one it was handed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a single ``check`` call.

    ``allowed`` — whether the caller is under the limit and may proceed.
    ``retry_after`` — seconds until the oldest hit inside the current
    window expires. Populated on deny so the middleware can set the
    ``Retry-After`` HTTP header; ``None`` on allow.
    """

    allowed: bool
    retry_after: float | None = None


class RateLimiter(Protocol):
    """Sliding-window counter keyed by an arbitrary string."""

    async def check(
        self, key: str, *, limit: int, window_seconds: float,
    ) -> RateLimitResult:
        """Record a hit on ``key`` and return whether it's within limit.

        Sliding semantics: a hit at time ``t`` counts toward the limit
        for every window that includes ``t``. If the latest hit would
        put the total count strictly above ``limit`` within the last
        ``window_seconds``, the call is denied *and not recorded* —
        denied calls must not consume quota, otherwise a client hammering
        a blocked endpoint could indefinitely extend its own lockout.
        """
        ...

    async def close(self) -> None:
        """Release any external resources (Redis client, etc.)."""
        ...
