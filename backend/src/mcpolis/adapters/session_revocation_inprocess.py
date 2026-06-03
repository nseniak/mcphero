"""In-process ``SessionRevocationStore`` for standalone mode.

Single-process deny-list for logged-out session cookies. Entries carry
their own expiry and are garbage-collected lazily on every read, so
the dict size is bounded by (concurrent active sessions × 7 days).

Standalone mode runs one backend process at a time — no coordination
required. Cloud mode uses the Redis adapter instead.
"""
from __future__ import annotations

import time


class InProcessSessionRevocationStore:
    """Dict-backed deny-list keyed by ``jti`` → expiry timestamp."""

    def __init__(self) -> None:
        self._revoked: dict[str, float] = {}

    async def revoke(self, jti: str, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        self._revoked[jti] = time.time() + ttl_seconds

    async def is_revoked(self, jti: str) -> bool:
        # Lazy GC: drop the entry if it outlived its own TTL. Cheap
        # enough to do on every read given the dict stays small.
        expiry = self._revoked.get(jti)
        if expiry is None:
            return False
        if expiry < time.time():
            self._revoked.pop(jti, None)
            return False
        return True

    async def close(self) -> None:
        self._revoked.clear()
