"""Unit tests for ``InProcessSessionRevocationStore``."""
from __future__ import annotations

import asyncio

import pytest

from mcpolis.adapters.session_revocation_inprocess import (
    InProcessSessionRevocationStore,
)


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
