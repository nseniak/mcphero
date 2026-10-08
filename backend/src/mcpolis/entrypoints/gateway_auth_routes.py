"""The OAuth routes of the gateway's sign-in, as mounted on ``/mcp``,
``/admin-mcp`` and ``/admin-mcp/system`` (one shared provider).

The MCP SDK's routes, with open client registration on and every
request's size capped: ``/register`` and ``/authorize`` answer anonymous
callers, and the SDK reads whatever body (or query string) it is sent.
"""
from __future__ import annotations

from collections.abc import Callable

from mcp.server.auth.routes import REGISTRATION_PATH, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    MAX_SIGN_IN_REQUEST_BYTES,
    McpGatewayOAuthProvider,
)
from mcpolis.entrypoints.middleware.request_body_limit import RequestBodyLimit

# The server metadata: a GET of a fixed document, nothing to cap.
_METADATA_PATH = "/.well-known/oauth-authorization-server"


def _too_large(error: str) -> Callable[[], Response]:
    """The refusal of an oversized sign-in request: an OAuth error
    (``error``), HTTP 413."""

    def refusal() -> Response:
        return JSONResponse(
            {
                "error": error,
                "error_description": (
                    "the request is larger than "
                    f"{MAX_SIGN_IN_REQUEST_BYTES} bytes"
                ),
            },
            status_code=413,
        )

    return refusal


def _capped(route: Route) -> Route:
    """``route`` refusing a body or query string over
    ``MAX_SIGN_IN_REQUEST_BYTES``, before the SDK parses it."""
    if route.path == _METADATA_PATH:
        return route
    error = (
        "invalid_client_metadata"
        if route.path == REGISTRATION_PATH
        else "invalid_request"
    )
    return Route(
        route.path,
        endpoint=RequestBodyLimit(
            route.app, MAX_SIGN_IN_REQUEST_BYTES, _too_large(error),
        ),
        methods=list(route.methods or []),
    )


def gateway_auth_routes(
    provider: McpGatewayOAuthProvider, issuer_url: AnyHttpUrl,
) -> list[Route]:
    routes = create_auth_routes(
        provider=provider,
        issuer_url=issuer_url,
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    return [_capped(route) for route in routes]
