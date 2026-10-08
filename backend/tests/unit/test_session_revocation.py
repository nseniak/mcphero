"""Unit tests for the session deny-list stores."""
from __future__ import annotations

import asyncio
import time

import pytest

from mcpolis.adapters.session_revocation_inprocess import (
    InProcessSessionRevocationStore,
)
from mcpolis.adapters.session_revocation_redis import RedisSessionRevocationStore


@pytest.mark.asyncio
async def test_fresh_jti_is_not_revoked() -> None:
    store = InProcessSessionRevocationStore()
    assert await store.is_revoked("never-touched") is False


@pytest.mark.asyncio
async def test_revoke_then_check() -> None:
    store = InProcessSessionRevocationStore()
    await store.revoke("jti-abc", ttl_seconds=60)
    assert await store.is_revoked("jti-abc") is True


@pytest.mark.asyncio
async def test_revoke_is_per_jti() -> None:
    store = InProcessSessionRevocationStore()
    await store.revoke("jti-a", ttl_seconds=60)
    assert await store.is_revoked("jti-a") is True
    assert await store.is_revoked("jti-b") is False


@pytest.mark.asyncio
async def test_revoke_with_nonpositive_ttl_is_noop() -> None:
    """Negative/zero TTLs can't protect against anything — already expired."""
    store = InProcessSessionRevocationStore()
    await store.revoke("jti-a", ttl_seconds=0)
    await store.revoke("jti-b", ttl_seconds=-5)
    assert await store.is_revoked("jti-a") is False
    assert await store.is_revoked("jti-b") is False


@pytest.mark.asyncio
async def test_expired_entries_are_garbage_collected_on_read() -> None:
    """Dict must not grow unbounded — lazy GC drops outlived entries."""
    store = InProcessSessionRevocationStore()
    await store.revoke("jti-short", ttl_seconds=0.05)
    assert await store.is_revoked("jti-short") is True
    await asyncio.sleep(0.1)
    assert await store.is_revoked("jti-short") is False
    # Internal dict should have been pruned by the read above.
    assert "jti-short" not in store._revoked  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_close_clears_state() -> None:
    store = InProcessSessionRevocationStore()
    await store.revoke("jti-a", ttl_seconds=60)
    await store.close()
    assert await store.is_revoked("jti-a") is False


class SilentRedis:
    """A Redis that accepts the connection and never answers."""

    async def set(self, *args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    async def exists(self, *args: object, **kwargs: object) -> int:
        await asyncio.Event().wait()
        return 0


def make_redis_store_that_never_answers() -> RedisSessionRevocationStore:
    store = RedisSessionRevocationStore("redis://127.0.0.1:1/0")
    store._client = SilentRedis()  # type: ignore[assignment]
    return store


async def test_redis_check_that_never_answers_admits_after_a_bounded_wait() -> None:
    """Every dashboard request checks the deny-list: a silent Redis
    costs a bounded delay and admits (fail-open), never a hang."""
    store = make_redis_store_that_never_answers()
    started = time.monotonic()
    assert await asyncio.wait_for(store.is_revoked("jti"), timeout=5) is False
    assert time.monotonic() - started < 2


async def test_redis_revoke_that_never_answers_returns_after_a_bounded_wait() -> None:
    """A sign-out or org switch must not hang on a silent Redis."""
    store = make_redis_store_that_never_answers()
    started = time.monotonic()
    await asyncio.wait_for(store.revoke("jti", 60), timeout=5)
    assert time.monotonic() - started < 2
