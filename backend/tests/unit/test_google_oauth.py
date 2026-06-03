"""Tests for McpGatewayOAuthProvider — unit tests using in-memory state."""
from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
    StoredAuthCode,
    extract_email_from_id_token,
)
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateRepository,
    OAuthStateSnapshot,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from tests.unit.factories import make_runtime_manager


class InMemoryOAuthStateRepository(OAuthStateRepository):
    """Minimal in-memory stand-in: a single global snapshot."""

    def __init__(self) -> None:
        self._snapshot = OAuthStateSnapshot()

    async def load(self) -> OAuthStateSnapshot:
        return self._snapshot

    async def save(self, snapshot: OAuthStateSnapshot) -> None:
        self._snapshot = snapshot


def make_provider(
    config: SettingsConfig | None = None,
) -> McpGatewayOAuthProvider:
    policy = PolicyEngine(config or SettingsConfig())
    return McpGatewayOAuthProvider(
        google_client_id="test-google-client-id",
        google_client_secret="test-google-secret",
        server_url="http://localhost:8000",
        runtime_manager=make_runtime_manager(policy),
        state_repository=InMemoryOAuthStateRepository(),
    )


def make_client(client_id: str = "test-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl("http://localhost:3000/callback")],
    )


def make_auth_params() -> AuthorizationParams:
    return AuthorizationParams(
        state="test-state",
        scopes=None,
        code_challenge="test-challenge",
        redirect_uri=AnyUrl("http://localhost:3000/callback"),
        redirect_uri_provided_explicitly=True,
    )


def make_id_token(email: str) -> str:
    """Build a fake Google JWT ID token with the given email."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "sub": "12345"}).encode()
    ).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"fake-signature").rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.{sig.decode()}"


# --- Client registration ---


@pytest.mark.asyncio
async def test_register_and_get_client() -> None:
    provider = make_provider()
    client = make_client()
    await provider.register_client(client)

    loaded = await provider.get_client("test-client")
    assert loaded is not None
    assert loaded.client_id == "test-client"


@pytest.mark.asyncio
async def test_get_unknown_client_returns_none() -> None:
    provider = make_provider()
    assert await provider.get_client("unknown") is None


# --- Authorization ---


@pytest.mark.asyncio
async def test_authorize_returns_google_url() -> None:
    provider = make_provider()
    client = make_client()
    params = make_auth_params()

    url = await provider.authorize(client, params)

    assert "accounts.google.com" in url
    assert "test-google-client-id" in url
    assert "openid" in url
    assert "email" in url


@pytest.mark.asyncio
async def test_authorize_stores_pending_auth() -> None:
    provider = make_provider()
    client = make_client()
    params = make_auth_params()

    await provider.authorize(client, params)

    assert len(provider._pending_auths) == 1


# --- Google callback ---


@pytest.mark.asyncio
async def test_callback_invalid_state_raises() -> None:
    provider = make_provider()
    with pytest.raises(ValueError, match="Invalid or expired"):
        await provider.handle_google_callback("code", "bad-state")


@pytest.mark.asyncio
async def test_callback_success_returns_redirect() -> None:
    provider = make_provider()
    client = make_client()
    await provider.register_client(client)
    params = make_auth_params()

    await provider.authorize(client, params)
    # Extract the google_state from pending auths
    google_state = next(iter(provider._pending_auths))

    # Mock the Google token exchange
    fake_id_token = make_id_token("alice@test.com")
    mock_response = AsyncMock()
    mock_response.json = lambda: {"id_token": fake_id_token}
    mock_response.raise_for_status = lambda: None

    with patch("mcpolis.adapters.auth.mcp_gateway_oauth_provider.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        redirect_url = await provider.handle_google_callback("google-code", google_state)

    assert "http://localhost:3000/callback" in redirect_url
    assert "code=" in redirect_url
    assert "state=test-state" in redirect_url

    # An auth code should have been stored
    assert len(provider._auth_codes) == 1


@pytest.mark.asyncio
async def test_callback_unknown_user_denied_by_policy() -> None:
    """When policy has roles and users, an unlisted email is rejected."""
    provider = make_provider(
        config=SettingsConfig(
            roles={
                "admin": RoleDefinition(
                    is_admin=True,
                    settings=RoleSettings(),
                ),
            },
            users={
                "admin@test.com": UserDefinition(role="admin"),
            },
        )
    )
    client = make_client()
    await provider.register_client(client)
    params = make_auth_params()

    await provider.authorize(client, params)
    google_state = next(iter(provider._pending_auths))

    fake_id_token = make_id_token("nobody@test.com")
    mock_response = AsyncMock()
    mock_response.json = lambda: {"id_token": fake_id_token}
    mock_response.raise_for_status = lambda: None

    with patch("mcpolis.adapters.auth.mcp_gateway_oauth_provider.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        with pytest.raises(ValueError, match="not authorized"):
            await provider.handle_google_callback("google-code", google_state)


# --- Token exchange ---


@pytest.mark.asyncio
async def test_exchange_authorization_code() -> None:
    provider = make_provider()
    client = make_client()
    await provider.register_client(client)

    # Manually insert an auth code
    provider._auth_codes["test-code"] = StoredAuthCode(
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

    stored = await provider.load_authorization_code(client, "test-code")
    assert stored is not None

    token = await provider.exchange_authorization_code(client, stored)
    assert token.access_token
    assert token.refresh_token
    assert token.token_type == "Bearer"

    # Auth code should be consumed
    assert len(provider._auth_codes) == 0


# --- Access token verification ---


@pytest.mark.asyncio
async def test_load_access_token_returns_email_as_client_id() -> None:
    provider = make_provider()
    client = make_client()
    await provider.register_client(client)

    # Insert an auth code and exchange it
    provider._auth_codes["test-code"] = StoredAuthCode(
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

    stored = await provider.load_authorization_code(client, "test-code")
    assert stored is not None
    token = await provider.exchange_authorization_code(client, stored)

    # Verify the access token
    access = await provider.load_access_token(token.access_token)
    assert access is not None
    assert access.client_id == "alice@test.com"  # email!


@pytest.mark.asyncio
async def test_verify_token_delegates_to_load_access_token() -> None:
    provider = make_provider()
    client = make_client()
    await provider.register_client(client)

    provider._auth_codes["test-code"] = StoredAuthCode(
        client_id="test-client",
        user_email="bob@test.com",
        code_challenge="challenge",
        redirect_uri=AnyUrl("http://localhost:3000/callback"),
        redirect_uri_provided_explicitly=True,
        scopes=None,
        state="state",
        created_at=time.time(),
        expires_at=time.time() + 300,
    )

    stored = await provider.load_authorization_code(client, "test-code")
    assert stored is not None
    token = await provider.exchange_authorization_code(client, stored)

    # verify_token should work (used by BearerAuthBackend)
    access = await provider.verify_token(token.access_token)
    assert access is not None
    assert access.client_id == "bob@test.com"


@pytest.mark.asyncio
async def test_expired_access_token_returns_none() -> None:
    provider = make_provider()
    # Manually insert an expired token into the global namespace.
    from mcpolis.domain.ports.oauth_state_repository import StoredAccessToken

    await provider._ensure_loaded()
    provider._access_tokens["expired-token"] = StoredAccessToken(
        token="expired-token",
        client_id="test-client",
        user_email="alice@test.com",
        scopes=[],
        expires_at=int(time.time()) - 100,
    )

    assert await provider.load_access_token("expired-token") is None


# --- Refresh tokens ---


@pytest.mark.asyncio
async def test_refresh_token_rotation() -> None:
    provider = make_provider()
    client = make_client()
    await provider.register_client(client)

    provider._auth_codes["test-code"] = StoredAuthCode(
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

    stored = await provider.load_authorization_code(client, "test-code")
    assert stored is not None
    token = await provider.exchange_authorization_code(client, stored)
    assert token.refresh_token is not None

    # Load and exchange refresh token
    refresh = await provider.load_refresh_token(client, token.refresh_token)
    assert refresh is not None

    new_token = await provider.exchange_refresh_token(client, refresh, [])
    assert new_token.access_token != token.access_token
    assert new_token.refresh_token != token.refresh_token

    # Old refresh token should be gone
    assert await provider.load_refresh_token(client, token.refresh_token) is None


# --- Token namespace ---


@pytest.mark.asyncio
async def test_tokens_live_in_a_single_global_namespace() -> None:
    """Gateway tokens are user-scoped, not org-scoped: any token
    issued by the provider is visible regardless of the active
    ``current_org_id`` value."""
    from mcpolis.domain.ports.oauth_state_repository import StoredAccessToken
    from mcpolis.entrypoints.controllers.gateway_controller import (
        current_org_id,
    )

    provider = make_provider()
    await provider._ensure_loaded()

    provider._access_tokens["tok"] = StoredAccessToken(
        token="tok",
        client_id="alice@test.com",
        user_email="alice@test.com",
        scopes=[],
        expires_at=int(time.time()) + 3600,
    )

    for active in ("acme", "beta", "default"):
        ctx = current_org_id.set(active)
        try:
            assert await provider.load_access_token("tok") is not None
        finally:
            current_org_id.reset(ctx)


# --- ID token extraction ---


def test_extract_email_from_valid_id_token() -> None:
    token = make_id_token("test@example.com")
    assert extract_email_from_id_token(token) == "test@example.com"


def test_extract_email_from_invalid_token() -> None:
    assert extract_email_from_id_token("not.a.jwt") is None
    assert extract_email_from_id_token("") is None
    assert extract_email_from_id_token("one_part") is None
