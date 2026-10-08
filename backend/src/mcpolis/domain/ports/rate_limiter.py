"""RateLimiter port — sliding-window rate limiting primitive.

Implementations:
- ``InProcessRateLimiter`` (standalone mode): dict of per-key timestamp
  deques. Zero dependencies, single-process semantics.
- ``RedisRateLimiter`` (cloud mode): sliding window via ZSET (ZADD +
  ZREMRANGEBYSCORE + ZCARD) inside one Lua script. Shares state across
  every backend hitting the same Redis.

Both follow the exact same contract so ``RateLimitService`` doesn't
care which one it was handed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RateLimitBucket:
    """One counter a request is charged against.

    ``key`` names the counter (e.g. ``tool_call:org:<org_id>``);
    ``limit`` hits are allowed inside any ``window_seconds`` span.
    """

    key: str
    limit: int
    window_seconds: float


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a single ``check`` call.

    ``allowed`` — whether the caller is under every limit and may proceed.
    ``retry_after`` — on deny, seconds until the refusing bucket has room
    again (its oldest hit ages out); ``None`` on allow.
    ``exceeded`` — on deny, the bucket that refused. When several are
    full, the one with the longest wait, so ``retry_after`` is honest.
    """

    allowed: bool
    retry_after: float | None = None
    exceeded: RateLimitBucket | None = None


class RateLimiter(Protocol):
    """Sliding-window counters keyed by arbitrary strings."""

    async def check(self, *buckets: RateLimitBucket) -> RateLimitResult:
        """Charge one hit to every bucket, all or nothing.

        The hit is admitted only when every bucket is under its limit;
        it is then recorded in every bucket. A refused hit is recorded
        in *none* of them. Two reasons:

        * A client hammering a blocked endpoint must not extend its own
          lockout indefinitely.
        * When one request is charged to two buckets (a caller and its
          whole org), a refusal by one must not eat quota from the
          other — otherwise one runaway caller refused by its own limit
          would keep draining its teammates' shared org quota.
        """
        ...

    async def close(self) -> None:
        """Release any external resources (Redis client, etc.)."""
        ...
