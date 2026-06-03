"""UpstreamConfigService.remove_upstream must also delete any stored
OAuth tokens for that upstream, so the privacy promise ("tokens
deleted when a connection is removed") holds when an admin removes an
upstream (not just when a user clicks disconnect)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_config_service import (
    UpstreamConfigService,
)


def make_token() -> OAuthToken:
    return OAuthToken(
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(UTC),
        scopes=["read"],
        refresh_token_created_at=datetime.now(UTC),
    )


class _NoopUpstreamStore:
    async def remove(self, org_id: str, upstream_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_remove_upstream_deletes_stored_tokens(tmp_path: Path) -> None:
    connection_store = FileConnectionStore(tmp_path)
    org_id = "acme"
    upstream_id = "github"

    await connection_store.put_admin_token(
        org_id, upstream_id, make_token(), authorized_by="admin@acme.com"
    )
    await connection_store.put_user_token(
        org_id, "alice@acme.com", upstream_id, make_token()
    )
    await connection_store.put_user_token(
        org_id, "bob@acme.com", upstream_id, make_token()
    )
    # Token for a different upstream — must survive.
    await connection_store.put_user_token(
        org_id, "alice@acme.com", "slack", make_token()
    )

    cm = UpstreamClientManager([])
    registry = ToolRegistry([], cm)
    service = UpstreamConfigService(
        _NoopUpstreamStore(),  # type: ignore[arg-type]
        cm,
        registry,
        connection_store,
    )

    await service.remove_upstream(org_id, upstream_id)

    assert await connection_store.get_admin_token(org_id, upstream_id) is None
    assert (
        await connection_store.get_user_token(org_id, "alice@acme.com", upstream_id)
        is None
    )
    assert (
        await connection_store.get_user_token(org_id, "bob@acme.com", upstream_id)
        is None
    )
    # Unrelated upstream's token is untouched.
    assert (
        await connection_store.get_user_token(org_id, "alice@acme.com", "slack")
        is not None
    )


@pytest.mark.asyncio
async def test_delete_all_upstream_tokens_returns_count(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    org_id = "acme"

    await store.put_admin_token(org_id, "github", make_token(), authorized_by="a")
    await store.put_user_token(org_id, "alice", "github", make_token())
    await store.put_user_token(org_id, "bob", "github", make_token())
    await store.put_user_token(org_id, "alice", "slack", make_token())

    deleted = await store.delete_all_upstream_tokens(org_id, "github")
    assert deleted == 3

    deleted_again = await store.delete_all_upstream_tokens(org_id, "github")
    assert deleted_again == 0
