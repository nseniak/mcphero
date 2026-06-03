"""Slug-aware OAuth metadata + bearer middleware for cloud-mode MCP.

The MCP Python SDK advertises a static ``resource`` URL in OAuth
protected-resource metadata and a static ``resource_metadata`` URL in
the WWW-Authenticate header. In cloud mode that doesn't work: requests
come in at ``/mcp/<org-slug>/...`` but the static URLs point at the
unscoped ``/mcp`` base, so Claude Code completes OAuth for a resource
that doesn't have any upstreams and fails on the first MCP call.

This module rebuilds both responses per-request based on the
``current_org_slug`` ContextVar set by ``OrgContextMiddleware``.

Standalone mode never populates the slug, so the slug-aware handlers
degrade to exactly the behavior of the SDK's static ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from mcpolis.entrypoints.controllers.gateway_controller import current_org_slug


def _join_slug(base: str, slug: str, suffix: str = "") -> str:
    """Join ``base`` + optional ``slug`` + optional ``suffix`` cleanly.

    ``base`` is expected to end in ``/mcp`` or ``/admin-mcp`` (no trailing
    slash). ``suffix`` is appended after the slug with a leading slash.
    """
    core = f"{base}/{slug}" if slug else base
    if suffix:
        return f"{core}/{suffix.lstrip('/')}"
    return core


@dataclass
class SlugAwareProtectedResourceMetadataHandler:
    """Returns protected-resource metadata with a slug-aware ``resource``.

    ``base_url`` is the server root + mount prefix without the slug,
    e.g. ``https://app.example.com/mcp``. When the request came in
    via ``/mcp/<slug>/...``, the current slug is appended so the
    returned resource matches the URL Claude actually uses.
    """

    base_url: str
    authorization_servers: list[AnyHttpUrl]

    async def handle(self, request: Request) -> Response:
        del request
        slug = current_org_slug.get()
        resource = _join_slug(self.base_url, slug)
        payload: dict[str, Any] = {
            "resource": resource,
            "authorization_servers": [str(url) for url in self.authorization_servers],
        }
        return JSONResponse(
            payload,
            headers={"Cache-Control": "public, max-age=3600"},
        )


class SlugAwareRequireAuthMiddleware:
    """ASGI middleware like ``RequireAuthMiddleware`` but slug-aware.

    Replaces the SDK's static ``resource_metadata_url`` with one built
    from the current slug at 401-send time, so the WWW-Authenticate
    header points Claude at the slug-scoped metadata endpoint.
    """

    def __init__(
        self,
        app: Any,
        required_scopes: list[str],
        base_url: str,
    ) -> None:
        self.app = app
        self.required_scopes = required_scopes
        # Trim trailing slash once so ``_join_slug`` is simple.
        self.base_url = base_url.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        auth_user = scope.get("user")
        if not isinstance(auth_user, AuthenticatedUser):
            await self._send_auth_error(
                send,
                status_code=401,
                error="invalid_token",
                description="Authentication required",
            )
            return

        auth_credentials = scope.get("auth")
        for required_scope in self.required_scopes:
            if auth_credentials is None or required_scope not in auth_credentials.scopes:
                await self._send_auth_error(
                    send,
                    status_code=403,
                    error="insufficient_scope",
                    description=f"Required scope: {required_scope}",
                )
                return

        await self.app(scope, receive, send)

    async def _send_auth_error(
        self, send: Send, status_code: int, error: str, description: str,
    ) -> None:
        slug = current_org_slug.get()
        metadata_url = _join_slug(
            self.base_url, slug, ".well-known/oauth-protected-resource",
        )
        www_auth = (
            f'Bearer error="{error}", error_description="{description}", '
            f'resource_metadata="{metadata_url}"'
        )
        body = {"error": error, "error_description": description}
        body_bytes = json.dumps(body).encode()
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body_bytes)).encode()),
                (b"www-authenticate", www_auth.encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body_bytes,
        })
