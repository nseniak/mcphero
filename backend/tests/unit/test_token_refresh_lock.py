"""Tests for token refresh with distributed lock (Phase 4, Step 3)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from mcpolis.adapters.distributed_lock_noop import NoOpDistributedLock
from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.services.upstream_connection_service import (
    refresh_token_for_user,
)


def make_upstream(upstream_id: str = "test-mcp") -> UpstreamDefinition:
    return UpstreamDefinition(
        id=upstream_id,
        display_name="Test MCP",
        transport=TransportType.streamable_http,
        auth=UpstreamAuthConfig(
            mode=AuthMode.admin_oauth,
            client_id="client-id",
            client_secret="client-secret",
        ),
        http=HttpTransportConfig(url="http://localhost:9999/mcp"),
    )


def make_expiring_token() -> OAuthToken:
    """Token that expires in 5 minutes (within the 20-min refresh margin)."""
    return OAuthToken(
        access_token="old-access-token",
        refresh_token="refresh-token",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        scopes=[],
    )


@pytest.mark.asyncio
async def test_distributed_lock_prevents_duplicate_token_refresh() -> None:
    """When the lock is held by another backend, refresh is skipped."""
    upstream = make_upstream()
    connection_store = AsyncMock()
    connection_store.get_user_token = AsyncMock(return_value=make_expiring_token())

    # Lock that always fails to acquire (simulates another backend holding it)
    lock = AsyncMock()
    lock.acquire = AsyncMock(return_value=False)
    lock.release = AsyncMock()

    await refresh_token_for_user(
        org_id="org-1",
        upstream=upstream,
        user_id="admin",
        connection_store=connection_store,
        server_url="http://localhost:8080",
        distributed_lock=lock,
    )

    lock.acquire.assert_awaited_once()
    # Should NOT have checked tokens (skipped early)
    connection_store.get_user_token.assert_not_awaited()
    # Should NOT have released (never acquired)
    lock.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_refresh_acquires_and_releases_lock() -> None:
    """When the lock is acquired, refresh proceeds and lock is released."""
    upstream = make_upstream()
    token = make_expiring_token()
    connection_store = AsyncMock()
    connection_store.get_user_token = AsyncMock(return_value=token)

    lock = AsyncMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.release = AsyncMock()

    await refresh_token_for_user(
        org_id="org-1",
        upstream=upstream,
        user_id="admin",
        connection_store=connection_store,
        server_url="http://localhost:8080",
        distributed_lock=lock,
    )

    lock.acquire.assert_awaited_once()
    # Token was checked (refresh proceeded)
    connection_store.get_user_token.assert_awaited()
    # Lock was released
    lock.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_token_refresh_works_without_lock() -> None:
    """Without a distributed lock (standalone mode), refresh works normally."""
    upstream = make_upstream()
    # Token not close to expiry — refresh should be skipped
    token = OAuthToken(
        access_token="access",
        refresh_token="refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        scopes=[],
    )
    connection_store = AsyncMock()
    connection_store.get_user_token = AsyncMock(return_value=token)

    # No lock passed — standalone mode
    await refresh_token_for_user(
        org_id="org-1",
        upstream=upstream,
        user_id="admin",
        connection_store=connection_store,
        server_url="http://localhost:8080",
    )

    # Token was checked (no lock gating)
    connection_store.get_user_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_noop_lock_always_allows_refresh() -> None:
    """NoOp lock (standalone mode) always allows refresh to proceed."""
    upstream = make_upstream()
    token = make_expiring_token()
    connection_store = AsyncMock()
    connection_store.get_user_token = AsyncMock(return_value=token)

    lock = NoOpDistributedLock()

    await refresh_token_for_user(
        org_id="org-1",
        upstream=upstream,
        user_id="admin",
        connection_store=connection_store,
        server_url="http://localhost:8080",
        distributed_lock=lock,
    )

    # Token was checked (NoOp lock always acquires)
    connection_store.get_user_token.assert_awaited()
