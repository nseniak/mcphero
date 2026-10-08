"""Tests for the Phase 2d ``RateLimiter`` adapters.

Covers both in-process and Redis implementations via a shared fixture
helper so the same behavioural guarantees are exercised for both.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

import coredis
import pytest
import structlog

from mcpolis.adapters.rate_limiter_inprocess import InProcessRateLimiter
from mcpolis.adapters.rate_limiter_redis import (  # pyright: ignore[reportPrivateUsage]
    _CHECK_SCRIPT,
    RedisRateLimiter,
)
from mcpolis.domain.ports.rate_limiter import (
    RateLimitBucket,
    RateLimiter,
    RateLimitResult,
)
from tests.unit._loopback_mcp import free_port
from tests.unit.redis_fixture import require_redis


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


def make_bucket(key: str, limit: int, window_seconds: float) -> RateLimitBucket:
    return RateLimitBucket(key=key, limit=limit, window_seconds=window_seconds)


LimiterFactory = Callable[[Callable[[], float]], Awaitable[RateLimiter]]


async def _make_inprocess(now: Callable[[], float]) -> RateLimiter:
    return InProcessRateLimiter(now=now)


async def _make_redis(now: Callable[[], float]) -> RateLimiter:
    url = require_redis()
    return RedisRateLimiter(url, now=now)


def _limiter_ids(factory: LimiterFactory) -> str:
    return "inprocess" if factory is _make_inprocess else "redis"


# The Redis variants are always listed; without a reachable Redis they
# show up as SKIPPED (``-rs``) instead of silently vanishing. They are
# the only guard on the Lua script's all-or-nothing rule.
_LIMITER_FACTORIES: list[LimiterFactory] = [_make_inprocess, _make_redis]


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_blocks_after_threshold(make: LimiterFactory) -> None:
    limiter = await make(FakeClock())
    try:
        key = f"test:{uuid.uuid4().hex[:8]}"
        # 3 hits allowed, 4th must be denied. The clock never advances,
        # so all four checks land at the same instant — the window can
        # never slide and starvation can't flip the threshold.
        for _ in range(3):
            res = await limiter.check(make_bucket(key, 3, 5.0))
            assert res.allowed is True
            assert res.retry_after is None
        denied = await limiter.check(make_bucket(key, 3, 5.0))
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
            res = await limiter.check(make_bucket(key, 2, 1.0))
            assert res.allowed is True
        # Hit the limit repeatedly — each denial must leave the oldest
        # timestamp unchanged, so we can observe the window expiring
        # deterministically.
        for _ in range(5):
            res = await limiter.check(make_bucket(key, 2, 1.0))
            assert res.allowed is False
        clock.advance(1.1)
        # After the window, the original two hits have aged out and
        # the limiter should accept two fresh hits. If denied hits had
        # been recorded, the quota would still be saturated here.
        res = await limiter.check(make_bucket(key, 2, 1.0))
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
            assert (await limiter.check(make_bucket(key_a, 2, 5.0))).allowed
        assert not (await limiter.check(make_bucket(key_a, 2, 5.0))).allowed
        # Key B has consumed nothing yet and must admit its own fresh
        # quota even though key A is blocked.
        for _ in range(2):
            assert (await limiter.check(make_bucket(key_b, 2, 5.0))).allowed
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
        assert (await limiter.check(make_bucket(key, 2, 1.0))).allowed
        clock.advance(0.5)
        assert (await limiter.check(make_bucket(key, 2, 1.0))).allowed
        # Limit is now saturated.
        assert not (await limiter.check(make_bucket(key, 2, 1.0))).allowed
        # Wait until the FIRST hit ages out but the second one is
        # still inside the window. A sliding-window limiter should
        # accept one new hit; a fixed-window limiter would not.
        clock.advance(0.6)
        assert (await limiter.check(make_bucket(key, 2, 1.0))).allowed
    finally:
        await limiter.close()


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_hit_ages_out_exactly_at_the_window_edge(make: LimiterFactory) -> None:
    """Both adapters agree on the boundary: a hit stops counting the
    instant it is exactly ``window_seconds`` old (Redis's
    ``ZREMRANGEBYSCORE`` bound is inclusive)."""
    clock = FakeClock()
    limiter = await make(clock)
    try:
        key = f"test:{uuid.uuid4().hex[:8]}"
        assert (await limiter.check(make_bucket(key, 1, 1.0))).allowed
        clock.advance(1.0)
        assert (await limiter.check(make_bucket(key, 1, 1.0))).allowed
    finally:
        await limiter.close()


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_admitted_hit_is_charged_to_every_bucket(make: LimiterFactory) -> None:
    limiter = await make(FakeClock())
    try:
        wide = make_bucket(f"test-wide:{uuid.uuid4().hex[:8]}", 5, 5.0)
        narrow = make_bucket(f"test-narrow:{uuid.uuid4().hex[:8]}", 1, 5.0)
        assert (await limiter.check(wide, narrow)).allowed
        # The narrow bucket took the hit: it is now full on its own.
        assert not (await limiter.check(narrow)).allowed
        # The wide bucket took exactly one hit: four more fit.
        for _ in range(4):
            assert (await limiter.check(wide)).allowed
        assert not (await limiter.check(wide)).allowed
    finally:
        await limiter.close()


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_refusal_by_one_bucket_charges_no_bucket(make: LimiterFactory) -> None:
    """One request charged to a caller and its org: when the caller's
    own bucket refuses, the org bucket must not lose quota — otherwise
    one runaway caller would drain its teammates' shared quota."""
    limiter = await make(FakeClock())
    try:
        caller = make_bucket(f"test-caller:{uuid.uuid4().hex[:8]}", 2, 5.0)
        org = make_bucket(f"test-org:{uuid.uuid4().hex[:8]}", 3, 5.0)
        for _ in range(2):
            assert (await limiter.check(caller, org)).allowed
        for _ in range(10):
            refused = await limiter.check(caller, org)
            assert not refused.allowed
            assert refused.exceeded == caller
        # The org bucket holds the 2 admitted hits only: 1 more fits.
        assert (await limiter.check(org)).allowed
        assert not (await limiter.check(org)).allowed
    finally:
        await limiter.close()


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_exceeded_names_the_bucket_with_the_longest_wait(
    make: LimiterFactory,
) -> None:
    clock = FakeClock()
    limiter = await make(clock)
    try:
        short = make_bucket(f"test-short:{uuid.uuid4().hex[:8]}", 1, 10.0)
        long = make_bucket(f"test-long:{uuid.uuid4().hex[:8]}", 1, 30.0)
        assert (await limiter.check(short, long)).allowed
        clock.advance(5.0)
        refused = await limiter.check(short, long)
        assert not refused.allowed
        assert refused.exceeded == long
        assert refused.retry_after == pytest.approx(25.0, abs=0.01)
    finally:
        await limiter.close()


async def test_inprocess_sweep_forgets_keys_whose_hits_aged_out() -> None:
    """Keys nobody has hit for a window are dropped, so a caller
    rotating client IPs can't grow the in-memory map without bound."""
    clock = FakeClock()
    limiter = InProcessRateLimiter(now=clock)
    await limiter.check(make_bucket("test:old", 5, 1.0))
    clock.advance(2.0)
    live = make_bucket("test:live", 10_000, 1.0)
    for _ in range(1_000):
        await limiter.check(live)
    assert "test:old" not in limiter._hits  # pyright: ignore[reportPrivateUsage]
    assert "test:live" in limiter._hits  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("make", _LIMITER_FACTORIES, ids=_limiter_ids)
async def test_wait_never_exceeds_the_window_after_a_clock_jump(
    make: LimiterFactory,
) -> None:
    clock = FakeClock()
    limiter = await make(clock)
    try:
        key = f"test:{uuid.uuid4().hex[:8]}"
        assert (await limiter.check(make_bucket(key, 1, 60.0))).allowed
        clock.advance(-100.0)  # wall clock stepped back
        refused = await limiter.check(make_bucket(key, 1, 60.0))
        assert not refused.allowed
        assert refused.retry_after is not None
        assert refused.retry_after <= 60.0
    finally:
        await limiter.close()


# ── Redis only ────────────────────────────────────────────────────────


async def test_two_backends_racing_admit_exactly_the_limit() -> None:
    """Two backends on one Redis, 400 concurrent checks of a caller and
    an org bucket: exactly the caller limit is admitted, and the org is
    charged for admitted hits only (all-or-nothing under concurrency).

    A check that fails open admits, which is the fail-open rule working,
    not a double admit; so the test keeps machine load from causing one,
    and names it if one happens anyway:

    - each check gets 30 s, not the 0.5 s production cap, which 400
      queued checks can exceed on a loaded machine;
    - each backend has at most 16 checks in flight. Each check in flight
      holds its own Redis connection, so 400 at once opened 400 new
      loopback connections in one burst, and on macOS such a burst
      makes connects fail (``ETIMEDOUT``) whatever the timeout. 32 in
      flight still race the two backends on the same buckets."""
    url = require_redis()
    backend_a = RedisRateLimiter(url, timeout_seconds=30.0)
    backend_b = RedisRateLimiter(url, timeout_seconds=30.0)
    in_flight = {id(backend_a): asyncio.Semaphore(16), id(backend_b): asyncio.Semaphore(16)}
    tag = uuid.uuid4().hex[:8]
    caller = make_bucket(f"test:race:caller:{tag}", 60, 60.0)
    org = make_bucket(f"test:race:org:{tag}", 100, 60.0)

    async def check(backend: RedisRateLimiter) -> RateLimitResult:
        async with in_flight[id(backend)]:
            return await backend.check(caller, org)

    try:
        with structlog.testing.capture_logs() as logs:
            results = await asyncio.gather(*[
                check(backend_a if i % 2 else backend_b) for i in range(400)
            ])
        inspector: coredis.Redis[str] = coredis.Redis.from_url(url, decode_responses=True)
        org_hits = await inspector.zcard(f"mcpolis:ratelimit:{org.key}")
        failed_open = [
            line for line in logs if line["event"] == "rate_limit.check.failed_open"
        ]
        assert failed_open == [], "a check failed open, so its admit proves nothing"
        assert sum(1 for r in results if r.allowed) == 60
        assert org_hits == 60
    finally:
        await backend_a.close()
        await backend_b.close()


async def test_check_survives_redis_forgetting_the_script() -> None:
    """After a Redis restart the server no longer knows the script. A
    never-loaded variant of the script reproduces that without a
    server-wide SCRIPT FLUSH: the check must still decide correctly,
    not fail open."""
    limiter = RedisRateLimiter(require_redis())
    limiter._script = limiter._client.register_script(  # pyright: ignore[reportPrivateUsage]
        f"{_CHECK_SCRIPT}\n-- {uuid.uuid4().hex}",
    )
    key = f"test:noscript:{uuid.uuid4().hex[:8]}"
    try:
        with structlog.testing.capture_logs() as logs:
            first = await limiter.check(make_bucket(key, 1, 60.0))
            second = await limiter.check(make_bucket(key, 1, 60.0))
        assert first.allowed
        assert not second.allowed
        assert [line for line in logs if line["event"] == "rate_limit.check.failed_open"] == []
    finally:
        await limiter.close()


async def test_silent_redis_fails_open_within_the_timeout() -> None:
    """A Redis that accepts connections but never answers must cost a
    bounded delay per request, not a hang: every tool call waits on it."""
    async def never_answer(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(3600)

    server = await asyncio.start_server(never_answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    limiter = RedisRateLimiter(f"redis://127.0.0.1:{port}/0", timeout_seconds=0.3)
    try:
        started = time.monotonic()
        result = await asyncio.wait_for(
            limiter.check(make_bucket("test:silent", 5, 60.0)), timeout=5.0,
        )
        assert result.allowed
        assert time.monotonic() - started < 1.5
    finally:
        server.close()


async def test_redis_outage_fails_open_and_logs_once_a_minute() -> None:
    """While Redis is down every check fails open; one ERROR line (one
    Sentry event) per minute, not per request."""
    limiter = RedisRateLimiter(f"redis://127.0.0.1:{free_port()}/0", timeout_seconds=0.3)
    bucket = make_bucket("test:outage", 5, 60.0)
    with structlog.testing.capture_logs() as logs:
        results = [await limiter.check(bucket) for _ in range(50)]
    lines = [line for line in logs if line["event"] == "rate_limit.check.failed_open"]
    assert all(r.allowed for r in results)
    assert len(lines) == 1
    assert lines[0]["log_level"] == "error"


async def test_redis_recovery_is_logged_with_the_unlogged_failures() -> None:
    limiter = RedisRateLimiter(require_redis())
    # State left by an outage: failing, 7 failures swallowed after the
    # first ERROR line.
    limiter._failing = True  # pyright: ignore[reportPrivateUsage]
    limiter._failure_log.attempt("check")  # pyright: ignore[reportPrivateUsage]
    for _ in range(7):
        limiter._failure_log.attempt("check")  # pyright: ignore[reportPrivateUsage]
    try:
        with structlog.testing.capture_logs() as logs:
            bucket = make_bucket(f"test:{uuid.uuid4().hex[:8]}", 5, 60.0)
            assert (await limiter.check(bucket)).allowed
        recovered = [line for line in logs if line["event"] == "rate_limit.check.recovered"]
        assert len(recovered) == 1
        assert recovered[0]["unlogged_failures"] == 7
    finally:
        await limiter.close()
