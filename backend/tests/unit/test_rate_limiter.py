"""Tests for the Phase 2d ``RateLimiter`` adapters.

Covers both in-process and Redis implementations via a shared fixture
helper so the same behavioural guarantees are exercised for both.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest

from mcpolis.adapters.rate_limiter_inprocess import InProcessRateLimiter
from mcpolis.adapters.rate_limiter_redis import RedisRateLimiter
from mcpolis.domain.ports.rate_limiter import RateLimiter

from tests.unit.redis_fixture import redis_available, require_redis


class FakeClock:
    """Controllable clock injected into the limiters.

    The sliding-window assertions used to depend on wall-clock elapsed
    time between calls: e.g. three ``check`` round-trips were expected
    to land inside a 5 s window. Under ``make test-all`` CPU starvation
    those round-trips can span more than the window, the early hits age
    out, and the threshold assertion flips (the dominant unit flake in
    the load-induced repro). Driving time explicitly removes the
    wall-clock dependency entirely, so the tests are deterministic
    regardless of load. Real elapsed time is never consumed.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


LimiterFactory = Callable[[Callable[[], float]], Awaitable[RateLimiter]]


async def _make_inprocess(now: Callable[[], float]) -> RateLimiter:
    return InProcessRateLimiter(now=now)


async def _make_redis(now: Callable[[], float]) -> RateLimiter:
    url = require_redis()
    return RedisRateLimiter(url, now=now)


def _limiter_ids(factory: LimiterFactory) -> str:
    return "inprocess" if factory is _make_inprocess else "redis"


_LIMITER_FACTORIES: list[LimiterFactory] = [_make_inprocess]
if redis_available():
    _LIMITER_FACTORIES.append(_make_redis)


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_blocks_after_threshold(make: LimiterFactory) -> None:
    limiter = await make(FakeClock())
    try:
        key = f"test:{uuid.uuid4().hex[:8]}"
        # 3 hits allowed, 4th must be denied. The clock never advances,
        # so all four checks land at the same instant — the window can
        # never slide and starvation can't flip the threshold.
        for _ in range(3):
            res = await limiter.check(key, limit=3, window_seconds=5.0)
            assert res.allowed is True
            assert res.retry_after is None
        denied = await limiter.check(key, limit=3, window_seconds=5.0)
        assert denied.allowed is False
        assert denied.retry_after is not None
        assert denied.retry_after > 0
    finally:
        await limiter.close()


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_denied_hits_do_not_consume_quota(make: LimiterFactory) -> None:
    """A denied call must not be recorded — otherwise a client hammering
    a blocked endpoint would indefinitely extend its own lockout."""
    clock = FakeClock()
    limiter = await make(clock)
    try:
        key = f"test:{uuid.uuid4().hex[:8]}"
        for _ in range(2):
            res = await limiter.check(key, limit=2, window_seconds=1.0)
            assert res.allowed is True
        # Hit the limit repeatedly — each denial must leave the oldest
        # timestamp unchanged, so we can observe the window expiring
        # deterministically.
        for _ in range(5):
            res = await limiter.check(key, limit=2, window_seconds=1.0)
            assert res.allowed is False
        clock.advance(1.1)
        # After the window, the original two hits have aged out and
        # the limiter should accept two fresh hits. If denied hits had
        # been recorded, the quota would still be saturated here.
        res = await limiter.check(key, limit=2, window_seconds=1.0)
        assert res.allowed is True
    finally:
        await limiter.close()


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_keys_are_independent(make: LimiterFactory) -> None:
    # No clock advance — key A's hits never age out, so the threshold
    # assertion can't flip on wall-clock drift under load.
    limiter = await make(FakeClock())
    try:
        key_a = f"test-a:{uuid.uuid4().hex[:8]}"
        key_b = f"test-b:{uuid.uuid4().hex[:8]}"
        for _ in range(2):
            assert (await limiter.check(
                key_a, limit=2, window_seconds=5.0,
            )).allowed
        assert not (await limiter.check(
            key_a, limit=2, window_seconds=5.0,
        )).allowed
        # Key B has consumed nothing yet and must admit its own fresh
        # quota even though key A is blocked.
        for _ in range(2):
            assert (await limiter.check(
                key_b, limit=2, window_seconds=5.0,
            )).allowed
    finally:
        await limiter.close()


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_window_slides(make: LimiterFactory) -> None:
    """Hits older than the window must age out — not wait for a
    fixed-window boundary. This is the distinguishing property of a
    sliding-window limiter versus a fixed-window limiter."""
    clock = FakeClock()
    limiter = await make(clock)
    try:
        key = f"test:{uuid.uuid4().hex[:8]}"
        assert (await limiter.check(
            key, limit=2, window_seconds=1.0,
        )).allowed
        clock.advance(0.5)
        assert (await limiter.check(
            key, limit=2, window_seconds=1.0,
        )).allowed
        # Limit is now saturated.
        assert not (await limiter.check(
            key, limit=2, window_seconds=1.0,
        )).allowed
        # Wait until the FIRST hit ages out but the second one is
        # still inside the window. A sliding-window limiter should
        # accept one new hit; a fixed-window limiter would not.
        clock.advance(0.6)
        assert (await limiter.check(
            key, limit=2, window_seconds=1.0,
        )).allowed
    finally:
        await limiter.close()
