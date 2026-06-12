"""Org pinning for service-token gateway requests.

A service token is bound to exactly one org at mint time; the pinned
org rides in the auth scopes (see ``service_token_verifier``). This
middleware enforces the pin on the ``/mcp`` sub-app:

- bare ``/mcp`` (``MULTI_ORG_SENTINEL``) → resolve to the pinned org.
  No email-based org fan-out ever runs for ``svc:`` identities (the
  prefetch middleware downstream sees a non-sentinel org and skips).
- ``/mcp/{slug}`` resolving to a different org → 401 with the same
  anti-enumeration body ``OrgContextMiddleware`` uses, so a token
  holder can't probe which slugs exist.

Ordering is load-bearing: must run after ``AuthContextMiddleware``
(needs ``auth_context_var``) and before both
``_GatewayLogContextBindMiddleware`` (so logs bind the pinned org)
and ``_MultiOrgUserOrgsPrefetchMiddleware`` (so
``list_user_orgs("svc:…")`` never runs).
"""
from __future__ import annotations

import structlog
from mcp.server.auth.middleware.auth_context import auth_context_var
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcpolis.domain.model.service_token import pinned_org_from_auth_scopes
from mcpolis.domain.ports import MULTI_ORG_SENTINEL
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class ServiceTokenOrgPinMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        auth_user = auth_context_var.get(None)
        if auth_user is None:
            await self._app(scope, receive, send)
            return
        pinned_org = pinned_org_from_auth_scopes(
            auth_user.access_token.scopes,
        )
        if pinned_org is None:
            # Human auth — untouched.
            await self._app(scope, receive, send)
            return

        try:
            org_id = current_org_id.get()
        except LookupError:
            org_id = ""

        if org_id == MULTI_ORG_SENTINEL:
            # Bare /mcp: resolve to the token's org instead of the
            # email-based fan-out humans get.
            token = current_org_id.set(pinned_org)
            structlog.contextvars.bind_contextvars(org_id=pinned_org)
            try:
                await self._app(scope, receive, send)
            finally:
                current_org_id.reset(token)
            return

        if org_id != pinned_org:
            # Same body + status as OrgContextMiddleware._reject —
            # anti-enumeration: don't reveal whether the slug exists.
            logger.info(
                "service_token.org_pin.rejected",
                user_id=auth_user.display_name,
            )
            response = JSONResponse(
                {"error": "Not authorized for this org"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)
