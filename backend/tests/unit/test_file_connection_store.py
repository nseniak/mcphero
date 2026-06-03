from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.domain.ports import DEFAULT_ORG_ID


def make_oauth_token(
    access_token: str = "access-123",
    refresh_token: str | None = "refresh-456",
    expires_at: datetime | None = None,
    scopes: list[str] | None = None,
) -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at or datetime(2026, 6, 1, tzinfo=UTC),
        scopes=scopes or ["read", "write"],
    )


@pytest.mark.asyncio
async def test_admin_token_roundtrip(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    token = make_oauth_token()

    assert await store.get_admin_token(DEFAULT_ORG_ID,"github") is None

    await store.put_admin_token(DEFAULT_ORG_ID,"github", token, authorized_by="admin@co.com")
    result = await store.get_admin_token(DEFAULT_ORG_ID,"github")

    assert result is not None
    assert result.access_token == "access-123"
    assert result.refresh_token == "refresh-456"
    assert result.scopes == ["read", "write"]


@pytest.mark.asyncio
async def test_user_token_roundtrip(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    token = make_oauth_token(access_token="user-token")

    assert await store.get_user_token(DEFAULT_ORG_ID,"alice@co.com", "slack") is None

    await store.put_user_token(DEFAULT_ORG_ID,"alice@co.com", "slack", token)
    result = await store.get_user_token(DEFAULT_ORG_ID,"alice@co.com", "slack")

    assert result is not None
    assert result.access_token == "user-token"


@pytest.mark.asyncio
async def test_delete_user_token(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    token = make_oauth_token()

    await store.put_user_token(DEFAULT_ORG_ID,"bob@co.com", "github", token)
    assert await store.get_user_token(DEFAULT_ORG_ID,"bob@co.com", "github") is not None

    await store.delete_user_token(DEFAULT_ORG_ID,"bob@co.com", "github")
    assert await store.get_user_token(DEFAULT_ORG_ID,"bob@co.com", "github") is None


@pytest.mark.asyncio
async def test_delete_nonexistent_token(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    # Should not raise
    await store.delete_user_token(DEFAULT_ORG_ID,"nobody@co.com", "github")


@pytest.mark.asyncio
async def test_separate_users_separate_tokens(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    token_alice = make_oauth_token(access_token="alice-token")
    token_bob = make_oauth_token(access_token="bob-token")

    await store.put_user_token(DEFAULT_ORG_ID,"alice@co.com", "github", token_alice)
    await store.put_user_token(DEFAULT_ORG_ID,"bob@co.com", "github", token_bob)

    alice_result = await store.get_user_token(DEFAULT_ORG_ID,"alice@co.com", "github")
    bob_result = await store.get_user_token(DEFAULT_ORG_ID,"bob@co.com", "github")

    assert alice_result is not None
    assert alice_result.access_token == "alice-token"
    assert bob_result is not None
    assert bob_result.access_token == "bob-token"


@pytest.mark.asyncio
async def test_admin_and_user_tokens_coexist(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    admin_token = make_oauth_token(access_token="admin-token")
    user_token = make_oauth_token(access_token="user-token")

    await store.put_admin_token(DEFAULT_ORG_ID,"github", admin_token, authorized_by="admin@co.com")
    await store.put_user_token(DEFAULT_ORG_ID,"alice@co.com", "github", user_token)

    admin_result = await store.get_admin_token(DEFAULT_ORG_ID,"github")
    user_result = await store.get_user_token(DEFAULT_ORG_ID,"alice@co.com", "github")

    assert admin_result is not None
    assert admin_result.access_token == "admin-token"
    assert user_result is not None
    assert user_result.access_token == "user-token"


@pytest.mark.asyncio
async def test_overwrite_token(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    token_v1 = make_oauth_token(access_token="v1")
    token_v2 = make_oauth_token(access_token="v2")

    await store.put_admin_token(DEFAULT_ORG_ID,"github", token_v1, authorized_by="admin@co.com")
    await store.put_admin_token(DEFAULT_ORG_ID,"github", token_v2, authorized_by="admin@co.com")

    result = await store.get_admin_token(DEFAULT_ORG_ID,"github")
    assert result is not None
    assert result.access_token == "v2"


@pytest.mark.asyncio
async def test_token_with_no_expiry(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    token = OAuthToken(
        access_token="access-123", refresh_token="refresh-456",
        expires_at=None, scopes=["read"],
    )

    await store.put_admin_token(DEFAULT_ORG_ID,"github", token, authorized_by="admin@co.com")
    result = await store.get_admin_token(DEFAULT_ORG_ID,"github")

    assert result is not None
    assert result.expires_at is None


@pytest.mark.asyncio
async def test_token_with_no_refresh(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    token = make_oauth_token(refresh_token=None)

    await store.put_user_token(DEFAULT_ORG_ID,"alice@co.com", "github", token)
    result = await store.get_user_token(DEFAULT_ORG_ID,"alice@co.com", "github")

    assert result is not None
    assert result.refresh_token is None


@pytest.mark.asyncio
async def test_persistence_across_instances(tmp_path: Path) -> None:
    store1 = FileConnectionStore(tmp_path)
    token = make_oauth_token()
    await store1.put_admin_token(DEFAULT_ORG_ID, "github", token, authorized_by="admin@co.com")

    store2 = FileConnectionStore(tmp_path)
    result = await store2.get_admin_token(DEFAULT_ORG_ID, "github")

    assert result is not None
    assert result.access_token == "access-123"
