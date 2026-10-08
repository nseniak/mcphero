"""Tests for DistributedLock implementations."""
from __future__ import annotations

import asyncio

import pytest

from mcpolis.adapters.distributed_lock_mongo import MongoDistributedLock
from mcpolis.adapters.distributed_lock_noop import NoOpDistributedLock
from mcpolis.adapters.repositories.mongo_client import MotorDatabase
from tests.unit.mongo_fixture import temp_mongo_database


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
# Each test takes its locks in a throwaway database of its own
# (``temp_mongo_database``). Every unit run on the host shares one Mongo
# daemon, and these tests used a fixed database with fixed keys: on
# 2026-10-07, with two ``make test-all`` runs going at once, one run's
# lock held "ttl-key" while the other run's lock tried to take it over,
# and test_mongo_lock_ttl_expires failed once per pair of runs.


def make_mongo_lock(db: MotorDatabase) -> MongoDistributedLock:
    return MongoDistributedLock(db)


@pytest.mark.asyncio
async def test_mongo_lock_acquire_and_release() -> None:
    async with temp_mongo_database() as db:
        lock = make_mongo_lock(db)
        try:
            assert await lock.acquire("test-key", ttl_seconds=10) is True
            # Same holder can re-acquire
            assert await lock.acquire("test-key", ttl_seconds=10) is True
            await lock.release("test-key")
        finally:
            await lock.close()


@pytest.mark.asyncio
async def test_mongo_lock_prevents_duplicate_acquisition() -> None:
    async with temp_mongo_database() as db:
        lock_a = make_mongo_lock(db)
        lock_b = make_mongo_lock(db)
        try:
            assert await lock_a.acquire("contended-key", ttl_seconds=10) is True
            # Different holder cannot acquire the same key
            assert await lock_b.acquire("contended-key", ttl_seconds=10) is False
            await lock_a.release("contended-key")
        finally:
            await lock_a.close()
            await lock_b.close()


@pytest.mark.asyncio
async def test_mongo_lock_release_allows_reacquire() -> None:
    async with temp_mongo_database() as db:
        lock_a = make_mongo_lock(db)
        lock_b = make_mongo_lock(db)
        try:
            assert await lock_a.acquire("release-key", ttl_seconds=10) is True
            await lock_a.release("release-key")
            # After release, another holder can acquire
            assert await lock_b.acquire("release-key", ttl_seconds=10) is True
            await lock_b.release("release-key")
        finally:
            await lock_a.close()
            await lock_b.close()


@pytest.mark.asyncio
async def test_mongo_lock_ttl_expires() -> None:
    """Lock with a very short TTL expires and can be taken over."""
    async with temp_mongo_database() as db:
        lock_a = make_mongo_lock(db)
        lock_b = make_mongo_lock(db)
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
