"""SessionRevocationStore port — deny-list for invalidated session cookies.

Session cookies are stateless HMAC-signed blobs with a 7-day TTL. A
signed cookie is otherwise valid for its entire TTL, even after the
user logs out — which means a stolen cookie can't be revoked. This
port gives ``/logout`` (and future sensitive events like role-grant
changes) a way to mark a cookie's ``jti`` as revoked until its own
``exp`` passes, after which the entry is garbage-collected.

Implementations:
- ``InProcessSessionRevocationStore`` (standalone): dict keyed by jti,
  pruned lazily on read. Single-process only.
- ``RedisSessionRevocationStore`` (cloud): Redis ``SET key "" EX ttl``
  so every backend hitting the same Redis sees the same deny list and
  entries expire on their own.

The verify-path consumer MUST fail-open on backend errors — a Redis
outage should degrade revocation to a no-op, not log every user out.
The cookie itself is still signed and time-bounded; revocation is a
defense-in-depth layer on top of that.
"""
from __future__ import annotations

from typing import Protocol


class SessionRevocationStore(Protocol):
    """Per-jti revocation deny-list with TTL-bounded entries."""

    async def revoke(self, jti: str, ttl_seconds: float) -> None:
        """Mark ``jti`` as revoked for at least ``ttl_seconds``.

        Implementations SHOULD clamp negative / zero TTLs to a no-op —
        already-expired cookies can't be replayed anyway.
        """
        ...

    async def is_revoked(self, jti: str) -> bool:
        """Return True if ``jti`` was revoked and is still within its TTL.

        Implementations MUST fail-open: on any backend error, log and
        return False rather than raising. Hot-path session verification
        calls this on every authenticated request.
        """
        ...

    async def close(self) -> None:
        """Release any external resources (Redis client, etc.)."""
        ...
