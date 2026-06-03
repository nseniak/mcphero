"""Google OAuth callback route — handles the redirect from Google after login."""
from __future__ import annotations

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import McpGatewayOAuthProvider

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


async def _handle_google_callback(request: Request) -> Response:
    provider: McpGatewayOAuthProvider = request.app.state.mcp_gateway_oauth_provider

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    logger.info(
        "google.oauth.callback.received",
        error=error,
        has_code=bool(code),
        has_state=bool(state),
    )

    if error:
        logger.warning("google.oauth.callback.failed", error=error)
        return JSONResponse(
            {"error": "google_auth_failed", "detail": error},
            status_code=400,
        )

    if not code or not state:
        return JSONResponse(
            {"error": "invalid_request", "detail": "Missing code or state parameter"},
            status_code=400,
        )

    try:
        redirect_url = await provider.handle_google_callback(code, state)
        logger.info(
            "google.oauth.callback.success",
            redirect_url_prefix=redirect_url[:100],
        )
        return RedirectResponse(url=redirect_url, status_code=302)
    except ValueError as e:
        logger.warning("google.oauth.callback.failed", error=str(e))
        return JSONResponse(
            {"error": "access_denied", "detail": str(e)},
            status_code=403,
        )


def create_google_callback_route() -> Route:
    return Route(
        "/oauth/google/callback",
        endpoint=_handle_google_callback,
        methods=["GET"],
    )
