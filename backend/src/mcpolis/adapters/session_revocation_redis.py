"""Redis-backed ``SessionRevocationStore`` for cloud mode.

One key per revoked ``jti`` with a per-key TTL matching the cookie's
remaining lifetime, so the deny-list garbage-collects itself. Shared
across every backend process hitting the same Redis instance.
"""
from __future__ import annotations

import math
from urllib.parse import urlparse

import coredis
import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_KEY_PREFIX = "mcpolis:session-revoked"


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


class RedisSessionRevocationStore:
    def __init__(self, url: str) -> None:
        self._client: coredis.Redis[str] = _client_from_url(url)

    async def revoke(self, jti: str, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        key = f"{_KEY_PREFIX}:{jti}"
        try:
            await self._client.set(
                key, "1", ex=max(1, math.ceil(ttl_seconds)),
            )
        except Exception:
            # Swallow revoke errors: the cookie is still signed + time-
            # bounded, and the user sees the client-side cookie deletion
            # regardless. Worst case the token remains honoured until
            # its own ``exp``, which is the pre-revocation behaviour.
            logger.exception(
                "session_revocation.revoke.failed",
                jti=jti,
            )

    async def is_revoked(self, jti: str) -> bool:
        key = f"{_KEY_PREFIX}:{jti}"
        try:
            return (await self._client.exists([key])) > 0
        except Exception:
            # Fail-open on the verify path — a Redis blip must never
            # log every user out. HMAC + exp remain in force.
            logger.exception(
                "session_revocation.is_revoked.failed_open",
                jti=jti,
            )
            return False

    async def close(self) -> None:
        try:
            self._client.connection_pool.disconnect()
        except Exception:
            logger.exception("session_revocation.close.failed")
