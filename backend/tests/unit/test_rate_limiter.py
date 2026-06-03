"""Tests for the Phase 2d ``RateLimiter`` adapters.

Covers both in-process and Redis implementations via a shared fixture
helper so the same behavioural guarantees are exercised for both.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest

from mcpolis.adapters.rate_limiter_inprocess import InProcessRateLimiter
from mcpolis.adapters.rate_limiter_redis import RedisRateLimiter
from mcpolis.domain.ports.rate_limiter import RateLimiter

from tests.unit.redis_fixture import redis_available, require_redis


LimiterFactory = Callable[[], Awaitable[RateLimiter]]


async def _make_inprocess() -> RateLimiter:
    return InProcessRateLimiter()


async def _make_redis() -> RateLimiter:
    url = require_redis()
    return RedisRateLimiter(url)


def _limiter_ids(factory: LimiterFactory) -> str:
    return "inprocess" if factory is _make_inprocess else "redis"


_LIMITER_FACTORIES: list[LimiterFactory] = [_make_inprocess]
if redis_available():
    _LIMITER_FACTORIES.append(_make_redis)


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_blocks_after_threshold(make: LimiterFactory) -> None:
    limiter = await make()
    try:
        key = f"test:{uuid.uuid4().hex[:8]}"
        # 3 hits allowed, 4th must be denied.
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
    limiter = await make()
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
        await asyncio.sleep(1.1)
        # After the window, the original two hits have aged out and
        # the limiter should accept two fresh hits. If denied hits had
        # been recorded, the quota would still be saturated here.
        res = await limiter.check(key, limit=2, window_seconds=1.0)
        assert res.allowed is True
    finally:
        await limiter.close()


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_keys_are_independent(make: LimiterFactory) -> None:
    limiter = await make()
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
    limiter = await make()
    try:
        key = f"test:{uuid.uuid4().hex[:8]}"
        assert (await limiter.check(
            key, limit=2, window_seconds=1.0,
        )).allowed
        await asyncio.sleep(0.5)
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
        await asyncio.sleep(0.6)
        assert (await limiter.check(
            key, limit=2, window_seconds=1.0,
        )).allowed
    finally:
        await limiter.close()
