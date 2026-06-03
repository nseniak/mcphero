"""Upstream OAuth callback route — handles the redirect from an upstream OAuth provider.

The ``state`` query-param is a signed HMAC token that encodes
(upstream_id, user_id, original_state).  This allows the callback to
identify the pending auth flow without an in-memory lookup, surviving
server restarts.
"""
from __future__ import annotations

import html

import structlog
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from mcpolis.adapters.auth.hmac_token import verify_token
from mcpolis.adapters.auth.pending_auth import (
    STATE_TOKEN_MAX_AGE,
    PendingAuthCoordinator,
)
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.events import Event
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head><title>MCP Hero — Authorization Complete</title></head>
<body style="font-family: system-ui; max-width: 480px; margin: 80px auto; text-align: center;">
  <h2>Authorization successful</h2>
  <p>This window will close automatically.</p>
  <script>setTimeout(function() { window.close(); }, 1500);</script>
</body>
</html>
"""

ERROR_HTML = """<!DOCTYPE html>
<html>
<head><title>MCP Hero — Authorization Failed</title></head>
<body style="font-family: system-ui; max-width: 480px; margin: 80px auto; text-align: center;">
  <h2>Authorization failed</h2>
  <p>{{error}}</p>
  <p>This window will close automatically.</p>
  <script>setTimeout(function() { window.close(); }, 3000);</script>
</body>
</html>
"""


async def _handle_upstream_oauth_callback(request: Request) -> Response:
    coordinator: PendingAuthCoordinator = (
        request.app.state.auth_coordinator
    )
    connection_store: ConnectionStore = (
        request.app.state.connection_store
    )

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        logger.warning("upstream.oauth.callback.failed", error=error)
        # Try to extract org/upstream/user from state to notify via SSE
        if state:
            error_payload = verify_token(
                state, coordinator.signing_key, max_age=STATE_TOKEN_MAX_AGE
            )
            if error_payload:
                event_bus: EventStream | None = getattr(
                    request.app.state, "event_bus", None
                )
                if event_bus is not None:
                    user_email: str | None = error_payload.get("usr")
                    # Prefer the org from the signed state token (set at
                    # /authorize time). Falls back to the request's
                    # contextvar for backwards compatibility with state
                    # tokens minted before the ``org`` field was added.
                    org_id = error_payload.get("org") or current_org_id.get()
                    event_bus.publish(org_id, Event(
                        type="upstream_oauth_error",
                        user_email=user_email,
                        payload={
                            "upstream_id": error_payload["uid"],
                            "error": error,
                        },
                    ))
        return HTMLResponse(
            ERROR_HTML.replace("{{error}}", html.escape(error, quote=True)),
            status_code=200,
        )

    if not code or not state:
        return JSONResponse(
            {
                "error": "invalid_request",
                "detail": "Missing code or state parameter",
            },
            status_code=400,
        )

    # Verify the signed state token
    payload = verify_token(
        state, coordinator.signing_key, max_age=STATE_TOKEN_MAX_AGE
    )
    if payload is None:
        return JSONResponse(
            {
                "error": "invalid_state",
                "detail": "Invalid or expired OAuth state",
            },
            status_code=400,
        )

    upstream_id: str = payload["uid"]
    user_id: str = payload["usr"]
    original_state: str = payload.get("ost", "")
    # Prefer the org from the signed state token (set at /authorize
    # time). Falls back to the request's contextvar if the token was
    # minted before the ``org`` field existed.
    org_id: str = payload.get("org") or current_org_id.get()

    # Fast path: if the PendingAuth is still in memory, signal it directly.
    # This completes the MCP SDK's callback_handler immediately.
    pending = coordinator.complete_by_key(
        org_id, upstream_id, user_id, code, original_state
    )

    if pending is not None:
        logger.info(
            "upstream.oauth.callback.in_memory",
            org_id=org_id,
            upstream_id=upstream_id,
            user=user_id,
        )
    else:
        # Slow path: server restarted between redirect and callback.
        # Defense-in-depth: re-verify that upstream_id refers to a real
        # upstream in this org before persisting the code. The signed
        # state token already attests to upstream_id, but validating
        # against the registry means a forged state (e.g. from a leaked
        # signing key) can't write pending codes for made-up upstreams.
        runtime_manager: OrgRuntimeManager = (
            request.app.state.runtime_manager
        )
        runtime = await runtime_manager.get(org_id)
        registered = await runtime.config_service.get_upstream(
            org_id, upstream_id
        )
        if registered is None:
            logger.warning(
                "upstream.oauth.callback.unknown_upstream",
                org_id=org_id,
                upstream_id=upstream_id,
                user=user_id,
            )
            return JSONResponse(
                {
                    "error": "invalid_state",
                    "detail": "Unknown upstream",
                },
                status_code=400,
            )
        # Store the code on disk so the next polling connect picks it up.
        logger.info(
            "upstream.oauth.callback.stored_on_disk",
            org_id=org_id,
            upstream_id=upstream_id,
            user=user_id,
        )
        await connection_store.put_pending_code(
            org_id, upstream_id, user_id, code, original_state
        )

    return HTMLResponse(SUCCESS_HTML, status_code=200)


def create_upstream_oauth_callback_route() -> Route:
    return Route(
        "/api/oauth/upstream/callback",
        endpoint=_handle_upstream_oauth_callback,
        methods=["GET"],
    )
