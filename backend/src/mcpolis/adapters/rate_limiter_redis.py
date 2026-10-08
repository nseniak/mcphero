"""Redis-backed ``RateLimiter`` used by cloud mode.

Sliding-window log implemented with a ZSET per key:

* Each hit is stored as a ZSET member whose score is the hit's time.
  The member value is unique (``{now}:{instance}:{counter}``) so two
  concurrent hits at the exact same score don't collapse.
* On every ``check`` call, for each bucket we:
  1. ``ZREMRANGEBYSCORE`` drops entries older than ``now - window``.
  2. ``ZCARD`` reads the current count.
  3. If at or over the limit, ``ZRANGE 0 0 WITHSCORES`` fetches the
     oldest surviving entry to compute ``Retry-After``.
* Only when every bucket has room do we ``ZADD`` the new entry to every
  bucket and refresh each TTL. A refusal writes nothing anywhere — same
  all-or-nothing semantics as the in-process adapter.

The whole sequence runs inside a Lua script so it's atomic across
Redis clients. Lua avoids pipelining races where two backends observe
``ZCARD == limit - 1`` and both admit themselves, and makes the
multi-bucket all-or-nothing rule hold under concurrency.

Every check is capped at ``timeout_seconds`` and fails open past it:
every gateway tool call waits on this, so a Redis that accepts
connections but never answers must cost a bounded delay, not a hang.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from typing import cast
from urllib.parse import urlparse

import coredis
import structlog

from mcpolis.domain.ports.rate_limiter import RateLimitBucket, RateLimitResult
from mcpolis.domain.services.emit_throttle import EmitThrottle

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_KEY_PREFIX = "mcpolis:ratelimit"

# Upper bound on one check, connect included. A healthy check is one
# round trip to Redis; this only bites when Redis is stuck.
_DEFAULT_TIMEOUT_SECONDS = 0.5

# While Redis is down every check fails open; log that once a minute
# (with a count), not once per request: each ERROR line is a Sentry event.
_FAILURE_LOG_INTERVAL_SECONDS = 60.0
_FAILURE_KEY = "check"

# KEYS[i]          = full ZSET key of bucket i
# ARGV[1]          = now (float seconds)
# ARGV[2]          = unique member value
# ARGV[1 + 2i]     = window of bucket i (float seconds)
# ARGV[2 + 2i]     = limit of bucket i (int)
# Returns: {allowed (0|1), retry_after (string seconds, "0" on allow),
#           index of the refusing bucket (1-based, 0 on allow)}
_CHECK_SCRIPT = """
local now = tonumber(ARGV[1])
local member = ARGV[2]

local worst_retry = -1
local worst_index = 0
for i, key in ipairs(KEYS) do
  local window = tonumber(ARGV[1 + 2 * i])
  local limit = tonumber(ARGV[2 + 2 * i])
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  local count = redis.call('ZCARD', key)
  if count >= limit then
    local retry = 0
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if oldest[2] ~= nil then
      retry = (tonumber(oldest[2]) + window) - now
      if retry < 0 then retry = 0 end
      -- A backwards clock jump can't promise more than one window.
      if retry > window then retry = window end
    end
    if retry > worst_retry then
      worst_retry = retry
      worst_index = i
    end
  end
end

if worst_index > 0 then
  return {0, tostring(worst_retry), worst_index}
end

for i, key in ipairs(KEYS) do
  local window = tonumber(ARGV[1 + 2 * i])
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000)
end
return {1, '0', 0}
"""


def _client_from_url(url: str, timeout_seconds: float) -> coredis.Redis[str]:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    db = 0
    if parsed.path and parsed.path != "/":
        try:
            db = int(parsed.path.lstrip("/"))
        except ValueError:
            db = 0
    password = parsed.password
    return coredis.Redis(
        host=host, port=port, db=db, password=password,
        decode_responses=True,
        connect_timeout=timeout_seconds,
        stream_timeout=timeout_seconds,
    )


class RedisRateLimiter:
    def __init__(
        self,
        url: str,
        *,
        now: Callable[[], float] = time.time,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client: coredis.Redis[str] = _client_from_url(url, timeout_seconds)
        self._timeout_seconds = timeout_seconds
        self._failure_log = EmitThrottle(_FAILURE_LOG_INTERVAL_SECONDS)
        self._failing = False
        self._script = self._client.register_script(_CHECK_SCRIPT)
        # Unique ZSET member suffix: a per-instance token plus a counter,
        # so hits from two backends at the same instant stay distinct.
        self._instance = secrets.token_hex(4)
        self._counter = 0
        # Injectable clock — the Lua script receives ``now`` as ARGV, so
        # the whole sliding window is driven by this. Tests inject a
        # controllable clock so the window can't slide on wall-clock
        # elapsed time between starved round-trips. Prod uses ``time.time``.
        self._now = now

    async def check(self, *buckets: RateLimitBucket) -> RateLimitResult:
        if not buckets:
            return RateLimitResult(allowed=True)
        now = self._now()
        self._counter = (self._counter + 1) % 1_000_000
        member = f"{now}:{self._instance}:{self._counter}"
        args = [str(now), member]
        for bucket in buckets:
            args += [str(bucket.window_seconds), str(bucket.limit)]
        try:
            async with asyncio.timeout(self._timeout_seconds):
                raw = await self._script.execute(
                    keys=[f"{_KEY_PREFIX}:{b.key}" for b in buckets],
                    args=tuple(args),
                )
        except Exception:
            # Fail open on Redis errors and timeouts. A Redis blip
            # shouldn't take down the whole gateway — we prefer a
            # temporarily unlimited endpoint to a blanket 500 or a hang.
            self._failing = True
            swallowed = self._failure_log.attempt(_FAILURE_KEY)
            if swallowed is not None:
                logger.exception(
                    "rate_limit.check.failed_open",
                    rate_limit_keys=[b.key for b in buckets],
                    failed_open_since_last_log=swallowed,
                )
            return RateLimitResult(allowed=True)
        if self._failing:
            self._failing = False
            logger.info(
                "rate_limit.check.recovered",
                unlogged_failures=self._failure_log.forget(_FAILURE_KEY),
            )

        allowed, retry_str, index = _parse_script_result(raw)
        if allowed:
            return RateLimitResult(allowed=True)
        exceeded = buckets[index - 1] if 1 <= index <= len(buckets) else buckets[0]
        try:
            retry_after = float(retry_str)
        except ValueError:
            retry_after = exceeded.window_seconds
        return RateLimitResult(
            allowed=False, retry_after=retry_after, exceeded=exceeded,
        )

    async def close(self) -> None:
        try:
            self._client.connection_pool.disconnect()
        except Exception:
            logger.exception("rate_limit.close.failed")


def _parse_script_result(raw: object) -> tuple[bool, str, int]:
    """Normalise the Lua script return value.

    The Lua script returns a three-element table
    ``{allowed, retry_str, index}``. coredis surfaces that as a
    ``list`` / ``tuple`` whose element types depend on the encoding
    path. Normalise to ``(bool, str, int)`` without caring which
    concrete container coredis used. On anything unexpected, fail open
    — a broken script result should not wedge the whole gateway.
    """
    if not isinstance(raw, list | tuple):
        logger.warning(
            "rate_limit.script_result.unexpected_shape",
            raw=repr(raw),
        )
        return True, "0", 0
    items = cast(tuple[object, ...], tuple(cast(object, x) for x in raw))  # type: ignore[redundant-cast]
    if len(items) < 3:
        logger.warning(
            "rate_limit.script_result.unexpected_length",
            items=repr(items),
        )
        return True, "0", 0
    allowed = _as_int(items[0], default=1) == 1
    retry_raw = items[1]
    if isinstance(retry_raw, bytes):
        retry_str = retry_raw.decode("utf-8", "replace")
    elif isinstance(retry_raw, str):
        retry_str = retry_raw
    else:
        retry_str = str(retry_raw)
    return allowed, retry_str, _as_int(items[2], default=0)


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, int | str | bytes):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default
