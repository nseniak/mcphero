"""Tests for McpGatewayOAuthProvider file-based state persistence."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
    PendingAuth,
    StoredAuthCode,
)
from mcpolis.adapters.repositories.file_oauth_state_repository import (
    FileOAuthStateRepository,
)
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.ports.oauth_state_repository import (
    StoredAccessToken,
    StoredRefreshToken,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from tests.unit.factories import make_runtime_manager


async def make_provider(tmp_path: Path) -> McpGatewayOAuthProvider:
    policy = PolicyEngine(SettingsConfig())
    repo = FileOAuthStateRepository(tmp_path)
    provider = McpGatewayOAuthProvider(
        google_client_id="test-google-id",
        google_client_secret="test-google-secret",
        server_url="http://localhost:8000",
        runtime_manager=make_runtime_manager(policy),
        state_repository=repo,
    )
    # Org state is loaded lazily on first access; no explicit load needed.
    return provider


def make_client(client_id: str = "test-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl("http://localhost:3000/callback")],
    )


@pytest.mark.asyncio
async def test_clients_survive_restart(tmp_path: Path) -> None:
    provider = await make_provider(tmp_path)
    client = make_client()
    await provider.register_client(client)

    # "Restart" — create a new provider from the same data_dir
    provider2 = await make_provider(tmp_path)
    loaded = await provider2.get_client("test-client")
    assert loaded is not None
    assert loaded.client_id == "test-client"


@pytest.mark.asyncio
async def test_tokens_survive_restart(tmp_path: Path) -> None:
    provider = await make_provider(tmp_path)
    client = make_client()
    await provider.register_client(client)

    # Exchange an auth code to create tokens
    provider._auth_codes["code1"] = StoredAuthCode(
        client_id="test-client",
        user_email="alice@test.com",
        code_challenge="challenge",
        redirect_uri=AnyUrl("http://localhost:3000/callback"),
        redirect_uri_provided_explicitly=True,
        scopes=None,
        state="state",
        created_at=time.time(),
        expires_at=time.time() + 300,
    )
    stored_code = await provider.load_authorization_code(client, "code1")
    assert stored_code is not None
    token = await provider.exchange_authorization_code(client, stored_code)

    # "Restart"
    provider2 = await make_provider(tmp_path)

    # Access token should survive
    access = await provider2.load_access_token(token.access_token)
    assert access is not None
    assert access.client_id == "alice@test.com"

    # Refresh token should survive
    assert token.refresh_token is not None
    client2 = await provider2.get_client("test-client")
    assert client2 is not None
    refresh = await provider2.load_refresh_token(client2, token.refresh_token)
    assert refresh is not None


@pytest.mark.asyncio
async def test_expired_access_tokens_not_loaded(tmp_path: Path) -> None:
    provider = await make_provider(tmp_path)
    await provider._ensure_loaded()
    provider._access_tokens["old"] = StoredAccessToken(
        token="old",
        client_id="test-client",
        user_email="alice@test.com",
        scopes=[],
        expires_at=int(time.time()) - 100,
    )
    await provider._save_state()  # pyright: ignore[reportPrivateUsage]

    # "Restart" — expired token should not be loaded
    provider2 = await make_provider(tmp_path)
    assert await provider2.load_access_token("old") is None


@pytest.mark.asyncio
async def test_expired_refresh_tokens_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mcpolis.adapters.repositories.file_oauth_state_repository.REFRESH_TOKEN_TTL",
        86400 * 30,
    )
    provider = await make_provider(tmp_path)
    await provider._ensure_loaded()
    provider._refresh_tokens["old"] = StoredRefreshToken(
        token="old",
        client_id="test-client",
        user_email="alice@test.com",
        scopes=[],
        created_at=time.time() - (86400 * 31),  # 31 days ago
    )
    await provider._save_state()  # pyright: ignore[reportPrivateUsage]

    # "Restart" — expired refresh token should not be loaded
    provider2 = await make_provider(tmp_path)
    await provider2._ensure_loaded()
    assert "old" not in provider2._refresh_tokens


@pytest.mark.asyncio
async def test_pending_auths_are_ephemeral(tmp_path: Path) -> None:
    provider = await make_provider(tmp_path)
    client = make_client()
    await provider.register_client(client)

    params = AuthorizationParams(
        state="s",
        scopes=None,
        code_challenge="c",
        redirect_uri=AnyUrl("http://localhost:3000/callback"),
        redirect_uri_provided_explicitly=True,
    )
    provider._pending_auths["state1"] = PendingAuth(
        client=client, params=params, google_state="state1"
    )

    # "Restart" — pending auths should be gone
    provider2 = await make_provider(tmp_path)
    assert len(provider2._pending_auths) == 0
