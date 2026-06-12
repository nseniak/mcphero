"""Service-token side of the gateway's bearer verification.

``CompositeGatewayTokenVerifier`` is what ``BearerAuthBackend`` wraps
on the ``/mcp`` app: ``svct_``-prefixed bearers go to the registry,
everything else to the untouched OAuth provider. The ``/admin-mcp``
app keeps wrapping the raw OAuth provider, so service tokens fail
there structurally.

The boundary-resolved (role, org) ride in the SDK-blessed channel —
``AccessToken.scopes`` — which downstream code reaches through
``auth_context_var``, the same channel identity already uses. The
scope encoding itself (constants + parse helpers) lives in
``domain/model/service_token.py``; this adapter mints it.
"""
from __future__ import annotations

from mcp.server.auth.provider import AccessToken, TokenVerifier

from mcpolis.domain.model.service_token import (
    SCOPE_ORG_PREFIX,
    SCOPE_ROLE_PREFIX,
    SCOPE_SVC,
    SERVICE_TOKEN_PREFIX,
    service_identity,
)
from mcpolis.domain.services.service_token_service import ServiceTokenService


class ServiceTokenVerifier:
    def __init__(self, service: ServiceTokenService) -> None:
        self._service = service

    async def verify_token(self, token: str) -> AccessToken | None:
        record = await self._service.verify(token)
        if record is None:
            return None
        return AccessToken(
            token=token,
            # client_id becomes AuthenticatedUser.display_name — the
            # identity string for policy, audit, and log context.
            client_id=service_identity(record.label),
            scopes=[
                SCOPE_SVC,
                SCOPE_ROLE_PREFIX + record.role_name,
                SCOPE_ORG_PREFIX + record.org_id,
            ],
            # None = non-expiring. The SDK's BearerAuthBackend uses a
            # truthiness check (``if auth_info.expires_at and ...``);
            # pinned by test_service_token_verifier.py against the
            # installed SDK so an upgrade can't silently break it.
            expires_at=None,
        )


class CompositeGatewayTokenVerifier:
    """Prefix-dispatching TokenVerifier for the gateway's bearer auth.

    Total dispatch: service tokens never touch the OAuth store, OAuth
    tokens never touch the registry.
    """

    def __init__(
        self,
        service_token_verifier: ServiceTokenVerifier,
        oauth_provider: TokenVerifier,
    ) -> None:
        self._svc = service_token_verifier
        self._oauth = oauth_provider

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith(SERVICE_TOKEN_PREFIX):
            return await self._svc.verify_token(token)
        return await self._oauth.verify_token(token)
