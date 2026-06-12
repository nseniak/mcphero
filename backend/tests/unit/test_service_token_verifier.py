from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken

from mcpolis.adapters.auth.service_token_verifier import (
    CompositeGatewayTokenVerifier,
    ServiceTokenVerifier,
)
from mcpolis.domain.model.service_token import (
    SCOPE_ORG_PREFIX,
    SCOPE_ROLE_PREFIX,
    SCOPE_SVC,
    boundary_role_from_auth_scopes,
    is_service_token_auth,
    pinned_org_from_auth_scopes,
)
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.services.service_token_service import ServiceTokenService


class _RefusingOAuthProvider:
    """Fails the test if the OAuth path is consulted."""

    async def verify_token(self, token: str) -> AccessToken | None:
        raise AssertionError(
            "OAuth provider must not see service tokens"
        )


class _RecordingOAuthProvider:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        self.seen.append(token)
        return None


def make_service(tmp_path: Path) -> ServiceTokenService:
    return ServiceTokenService(repo=FileServiceTokenRepository(tmp_path))


@pytest.mark.asyncio
async def test_composite_dispatches_svct_prefix_to_registry_not_oauth_store(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    minted = await service.mint(
        org_id="org-a", label="ci-bot", role_name="reader",
        created_by="admin@example.com",
    )
    composite = CompositeGatewayTokenVerifier(
        ServiceTokenVerifier(service),
        _RefusingOAuthProvider(),
    )
    access = await composite.verify_token(minted.raw_token)
    assert access is not None
    # Unknown svct_ tokens also stay on the registry path.
    assert await composite.verify_token("svct_unknown") is None


@pytest.mark.asyncio
async def test_composite_routes_oauth_tokens_past_registry(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    oauth = _RecordingOAuthProvider()
    composite = CompositeGatewayTokenVerifier(
        ServiceTokenVerifier(service), oauth,
    )
    assert await composite.verify_token("regular-oauth-token") is None
    assert oauth.seen == ["regular-oauth-token"]


@pytest.mark.asyncio
async def test_verify_builds_access_token_with_svc_identity_role_org_scopes_and_no_expiry(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    minted = await service.mint(
        org_id="org-a", label="ci-bot", role_name="reader",
        created_by="admin@example.com",
    )
    verifier = ServiceTokenVerifier(service)
    access = await verifier.verify_token(minted.raw_token)
    assert access is not None
    assert access.client_id == "svc:ci-bot"
    assert access.scopes == [
        SCOPE_SVC,
        SCOPE_ROLE_PREFIX + "reader",
        SCOPE_ORG_PREFIX + "org-a",
    ]
    # Pin the SDK contract: BearerAuthBackend treats expires_at via a
    # truthiness check, so None means non-expiring. If an SDK upgrade
    # changes that, this assertion is the tripwire.
    assert access.expires_at is None
    import inspect

    from mcp.server.auth.middleware import bearer_auth
    source = inspect.getsource(bearer_auth.BearerAuthBackend.authenticate)
    assert "expires_at and" in source


@pytest.mark.asyncio
async def test_revoked_token_verifies_to_none(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    minted = await service.mint(
        org_id="org-a", label="ci-bot", role_name="reader",
        created_by="admin@example.com",
    )
    verifier = ServiceTokenVerifier(service)
    assert await verifier.verify_token(minted.raw_token) is not None
    await service.revoke("org-a", "ci-bot")
    assert await verifier.verify_token(minted.raw_token) is None


def test_scope_helpers_roundtrip() -> None:
    scopes = [SCOPE_SVC, SCOPE_ROLE_PREFIX + "reader", SCOPE_ORG_PREFIX + "org-a"]
    assert is_service_token_auth(scopes)
    assert boundary_role_from_auth_scopes(scopes) == "reader"
    assert pinned_org_from_auth_scopes(scopes) == "org-a"
    # Human auth (no svc scope) yields None even if a stray scope
    # happens to carry the prefixes.
    human = [SCOPE_ROLE_PREFIX + "reader", SCOPE_ORG_PREFIX + "org-a"]
    assert not is_service_token_auth(human)
    assert boundary_role_from_auth_scopes(human) is None
    assert pinned_org_from_auth_scopes(human) is None
    assert boundary_role_from_auth_scopes([]) is None
