"""Redis-backed ``RateLimiter`` used by cloud mode.

Sliding-window log implemented with a ZSET per key:

* Each hit is stored as a ZSET member whose score is the server's
  monotonic time when the hit arrived. The member value is unique
  (``{score}:{nanos}``) so two concurrent hits at the exact same score
  don't collapse.
* On every ``check`` call we:
  1. ``ZREMRANGEBYSCORE`` drops entries older than ``now - window``.
  2. ``ZCARD`` reads the current count.
  3. If under the limit, ``ZADD`` the new entry and refresh the TTL.
  4. If at or over the limit, ``ZRANGEBYSCORE limit 0 1`` fetches the
     oldest surviving entry to compute ``Retry-After``.
* Denied calls do **not** call ``ZADD`` — same no-self-extension
  semantics as the in-process adapter.

The whole sequence runs inside a Lua script so it's atomic across
Redis clients. Lua avoids pipelining races where two backends observe
``ZCARD == limit - 1`` and both admit themselves.
"""
from __future__ import annotations

from typing import cast
from urllib.parse import urlparse

import coredis
import structlog

from mcpolis.domain.ports.rate_limiter import RateLimitResult

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_KEY_PREFIX = "mcpolis:ratelimit"

# KEYS[1] = full ZSET key
# ARGV[1] = now (float seconds)
# ARGV[2] = window (float seconds)
# ARGV[3] = limit (int)
# ARGV[4] = unique member value
# Returns: {allowed (0|1), retry_after (string of seconds, "0" on allow)}
_CHECK_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)

if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = 0
  if oldest[2] ~= nil then
    retry = (tonumber(oldest[2]) + window) - now
    if retry < 0 then retry = 0 end
  end
  return {0, tostring(retry)}
end

redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000)
return {1, '0'}
"""


def _client_from_url(url: str) -> coredis.Redis[str]:
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
    )


class RedisRateLimiter:
    def __init__(self, url: str) -> None:
        self._client: coredis.Redis[str] = _client_from_url(url)
        self._script = self._client.register_script(_CHECK_SCRIPT)
        self._counter = 0  # unique suffix for ZSET members

    async def check(
        self, key: str, *, limit: int, window_seconds: float,
    ) -> RateLimitResult:
        import time as _time
        now = _time.time()
        self._counter = (self._counter + 1) % 1_000_000
        member = f"{now}:{self._counter}"
        full_key = f"{_KEY_PREFIX}:{key}"
        try:
            raw = await self._script.execute(
                keys=[full_key],
                args=[str(now), str(window_seconds), str(limit), member],
            )
        except Exception:
            # Fail-open on Redis errors. A short Redis blip shouldn't
            # take down the whole gateway — we prefer a temporarily
            # unlimited endpoint to a blanket 500.
            logger.exception(
                "rate_limit.check.failed_open",
                rate_limit_key=key,
                limit=limit,
                window_seconds=window_seconds,
            )
            return RateLimitResult(allowed=True)

        allowed, retry_str = _parse_script_result(raw)
        if allowed:
            return RateLimitResult(allowed=True)
        try:
            retry_after = float(retry_str)
        except ValueError:
            retry_after = window_seconds
        return RateLimitResult(allowed=False, retry_after=retry_after)

    async def close(self) -> None:
        try:
            self._client.connection_pool.disconnect()
        except Exception:
            logger.exception("rate_limit.close.failed")


def _parse_script_result(raw: object) -> tuple[bool, str]:
    """Normalise the Lua script return value.

    The Lua script returns a two-element table ``{allowed, retry_str}``.
    coredis surfaces that as a ``list`` / ``tuple`` of two items whose
    element types depend on the encoding path. Normalise to
    ``(bool, str)`` without caring which concrete container coredis
    used. On anything unexpected, fail open — a broken script result
    should not wedge the whole gateway.
    """
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            "rate_limit.script_result.unexpected_shape",
            raw=repr(raw),
        )
        return True, "0"
    items = cast(tuple[object, ...], tuple(cast(object, x) for x in raw))  # type: ignore[redundant-cast]
    if len(items) < 2:
        logger.warning(
            "rate_limit.script_result.unexpected_length",
            items=repr(items),
        )
        return True, "0"
    allowed_raw = items[0]
    retry_raw = items[1]
    if isinstance(allowed_raw, (int, str, bytes)):
        try:
            allowed = int(allowed_raw) == 1
        except (TypeError, ValueError):
            allowed = True
    else:
        allowed = True
    if isinstance(retry_raw, bytes):
        retry_str = retry_raw.decode("utf-8", "replace")
    elif isinstance(retry_raw, str):
        retry_str = retry_raw
    else:
        retry_str = str(retry_raw)
    return allowed, retry_str
