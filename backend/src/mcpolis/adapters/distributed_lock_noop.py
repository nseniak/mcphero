"""No-op distributed lock for standalone (single-process) mode."""
from __future__ import annotations


class NoOpDistributedLock:
    """Always acquires — standalone mode needs no coordination."""

    async def acquire(self, key: str, ttl_seconds: float = 30) -> bool:
        return True

    async def release(self, key: str) -> None:
        pass

    async def close(self) -> None:
        pass
