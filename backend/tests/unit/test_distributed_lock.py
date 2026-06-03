"""Tests for DistributedLock implementations."""
from __future__ import annotations

import asyncio
import os

import pytest

from mcpolis.adapters.distributed_lock_mongo import MongoDistributedLock
from mcpolis.adapters.distributed_lock_noop import NoOpDistributedLock


# ---------------------------------------------------------------------------
# NoOp lock (standalone mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_lock_always_acquires() -> None:
    lock = NoOpDistributedLock()
    assert await lock.acquire("some-key") is True
    assert await lock.acquire("some-key") is True
    await lock.release("some-key")
    await lock.close()


@pytest.mark.asyncio
async def test_noop_lock_multiple_keys() -> None:
    lock = NoOpDistributedLock()
    assert await lock.acquire("key-a") is True
    assert await lock.acquire("key-b") is True
    await lock.release("key-a")
    await lock.release("key-b")


# ---------------------------------------------------------------------------
# Mongo lock (cloud mode) — requires a running Mongo instance
# ---------------------------------------------------------------------------

MONGO_URI = os.environ.get("MCPOLIS_TEST_MONGO_URI", "mongodb://localhost:27017")


def make_mongo_lock() -> MongoDistributedLock:
    from motor.motor_asyncio import AsyncIOMotorClient

    client: AsyncIOMotorClient = AsyncIOMotorClient(MONGO_URI)  # type: ignore[type-arg]
    db = client["mcpolis_test_locks"]
    return MongoDistributedLock(db)


async def _mongo_available() -> bool:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient

        client: AsyncIOMotorClient = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=1000)  # type: ignore[type-arg]
        await client.admin.command("ping")
        return True
    except Exception:
        return False


def _check_mongo_available() -> bool:
    """Run ``_mongo_available`` once at import time on a fresh loop.

    Uses ``asyncio.new_event_loop`` rather than ``get_event_loop``
    because the latter emits a DeprecationWarning under Python 3.12
    when called outside an active loop. The loop is closed before
    return so we don't leak it into pytest-asyncio's own scheduling.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_mongo_available())
    finally:
        loop.close()


mongo_required = pytest.mark.skipif(
    not _check_mongo_available(),
    reason="Mongo not reachable",
)


@mongo_required
@pytest.mark.asyncio
async def test_mongo_lock_acquire_and_release() -> None:
    lock = make_mongo_lock()
    try:
        assert await lock.acquire("test-key", ttl_seconds=10) is True
        # Same holder can re-acquire
        assert await lock.acquire("test-key", ttl_seconds=10) is True
        await lock.release("test-key")
    finally:
        await lock.close()


@mongo_required
@pytest.mark.asyncio
async def test_mongo_lock_prevents_duplicate_acquisition() -> None:
    lock_a = make_mongo_lock()
    lock_b = make_mongo_lock()
    try:
        assert await lock_a.acquire("contended-key", ttl_seconds=10) is True
        # Different holder cannot acquire the same key
        assert await lock_b.acquire("contended-key", ttl_seconds=10) is False
        await lock_a.release("contended-key")
    finally:
        await lock_a.close()
        await lock_b.close()


@mongo_required
@pytest.mark.asyncio
async def test_mongo_lock_release_allows_reacquire() -> None:
    lock_a = make_mongo_lock()
    lock_b = make_mongo_lock()
    try:
        assert await lock_a.acquire("release-key", ttl_seconds=10) is True
        await lock_a.release("release-key")
        # After release, another holder can acquire
        assert await lock_b.acquire("release-key", ttl_seconds=10) is True
        await lock_b.release("release-key")
    finally:
        await lock_a.close()
        await lock_b.close()


@mongo_required
@pytest.mark.asyncio
async def test_mongo_lock_ttl_expires() -> None:
    """Lock with a very short TTL expires and can be taken over."""
    lock_a = make_mongo_lock()
    lock_b = make_mongo_lock()
    try:
        # Acquire with a 1-second TTL
        assert await lock_a.acquire("ttl-key", ttl_seconds=1) is True
        # Wait for it to expire
        await asyncio.sleep(1.5)
        # The expired-lock takeover path in acquire should work
        assert await lock_b.acquire("ttl-key", ttl_seconds=10) is True
        await lock_b.release("ttl-key")
    finally:
        await lock_a.close()
        await lock_b.close()
