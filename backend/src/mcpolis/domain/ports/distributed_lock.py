"""DistributedLock port — cross-backend mutual exclusion.

Implementations:
- ``NoOpDistributedLock`` (standalone mode): always acquires, no-op
  release.  Single-process semantics make real locking unnecessary.
- ``MongoDistributedLock`` (cloud mode): TTL-indexed Mongo collection.
  Each backend generates a UUID at startup and uses it as the lock
  holder.  ``acquire`` upserts with ``$setOnInsert``; ``release``
  deletes the doc if the caller still holds it.

Used by ``SignInRefreshLock`` to prevent multiple backends from
refreshing the same sign-in's tokens concurrently.
"""
from __future__ import annotations

from typing import Protocol


class DistributedLock(Protocol):
    """Try-acquire / release lock on a string key."""

    async def acquire(self, key: str, ttl_seconds: float = 30) -> bool:
        """Try to acquire the lock.  Returns True on success. Acquiring a
        key this holder already holds succeeds and starts its
        ``ttl_seconds`` over: that is how a holder renews it."""
        ...

    async def release(self, key: str) -> None:
        """Release the lock (no-op if not held)."""
        ...

    async def close(self) -> None:
        """Release any external resources."""
        ...
