"""Per-user OAuth connect / disconnect (2 routes).

The user-tab Connect button (per-user OAuth flow) lives here. The
admin-tab Connect button (admin-OAuth single-slot) is a separate
route under ``upstream_admin.py``.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.domain.model.events import Event
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.services.upstream_connection_service import (
    OAuthFailureReason,
    connect_and_refresh_tools,
)
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import (
    DashboardDeps,
    notify_policy_change,
)
from mcpolis.entrypoints.routes.dashboard._models import ConnectResponse


def create_auth_connect_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["dashboard-connect"])

    @router.get("/connect/{upstream_id}", response_model=ConnectResponse)
    async def connect_upstream(
        upstream_id: str,
        email: str = Depends(deps.get_current_user),
    ) -> ConnectResponse:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        if deps.connection_store is None or deps.auth_coordinator is None:
            raise HTTPException(400, "OAuth is not configured")

        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")

        if upstream.auth.mode not in (
            AuthMode.per_user_oauth, AuthMode.admin_oauth,
        ):
            raise HTTPException(
                400,
                f"Upstream '{upstream_id}' does not use OAuth authentication",
            )

        def _notify_tokens_acquired() -> None:
            if deps.event_bus is not None:
                deps.event_bus.publish(org_id, Event(
                    type="upstream_tokens_acquired",
                    user_email=email,
                    payload={"upstream_id": upstream_id},
                ))
            # Slow path: tokens arrive via the OAuth callback. Notify
            # gateway sessions so connected MCP clients re-list tools.
            # Scope to the user for per_user_oauth; broadcast for
            # admin_oauth since that connection is shared.
            if upstream.auth.mode == AuthMode.per_user_oauth:
                notify_policy_change(deps, user=email)
            else:
                notify_policy_change(deps)
            provider_domain = (
                urlparse(upstream.http.url).netloc if upstream.http else ""
            )
            get_analytics().track_async(
                email,
                "upstream_oauth_completed",
                {
                    "upstream_id": upstream_id,
                    "auth_mode": upstream.auth.mode.value,
                    "oauth_provider_domain": provider_domain,
                },
            )

        def _notify_error(
            error_msg: str, reason: OAuthFailureReason,
        ) -> None:
            if deps.event_bus is not None:
                deps.event_bus.publish(org_id, Event(
                    type="upstream_oauth_error",
                    user_email=email,
                    payload={"upstream_id": upstream_id, "error": error_msg},
                ))
            get_analytics().track_async(
                email,
                "upstream_oauth_failed",
                {
                    "upstream_id": upstream_id,
                    "auth_mode": upstream.auth.mode.value,
                    "failure_reason": reason.value,
                },
            )

        def _on_tools_refreshed() -> None:
            # Re-broadcast after the background catalog refresh so the
            # dashboard's "Fetching info" indicator clears and the real
            # tool_count appears. Scoping mirrors the post-connect
            # notify_policy_change above.
            if upstream.auth.mode == AuthMode.per_user_oauth:
                notify_policy_change(deps, user=email)
            else:
                notify_policy_change(deps)

        result = await connect_and_refresh_tools(
            org_id=org_id,
            upstream=upstream,
            effective_user=email,
            connection_store=deps.connection_store,
            auth_coordinator=deps.auth_coordinator,
            client_manager=runtime.client_manager,
            tool_registry=runtime.tool_registry,
            server_url=deps.server_url,
            on_tokens_acquired=_notify_tokens_acquired,
            on_error=_notify_error,
            on_tools_refreshed=_on_tools_refreshed,
        )
        # Fast path: stored tokens already worked and connect_and_refresh
        # returned connected=True without triggering _notify_tokens_acquired.
        if result.connected:
            if upstream.auth.mode == AuthMode.per_user_oauth:
                notify_policy_change(deps, user=email)
            else:
                notify_policy_change(deps)
        elif result.error and not result.authorization_url:
            # Synchronous failure (timeout, post-refresh connection
            # error). _notify_error is only invoked from the async
            # token-acquisition task, so emit the analytics event here
            # to keep the failure dashboard whole.
            sync_reason = result.failure_reason or OAuthFailureReason.unknown
            get_analytics().track_async(
                email,
                "upstream_oauth_failed",
                {
                    "upstream_id": upstream_id,
                    "auth_mode": upstream.auth.mode.value,
                    "failure_reason": sync_reason.value,
                },
            )
        return ConnectResponse(
            authorization_url=result.authorization_url,
            connected=result.connected,
            error=result.error,
        )

    @router.post("/disconnect/{upstream_id}")
    async def disconnect_upstream(
        upstream_id: str,
        email: str = Depends(deps.get_current_user),
    ) -> dict[str, str]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        if deps.connection_store is None:
            raise HTTPException(400, "OAuth is not configured")

        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")

        await deps.connection_store.delete_user_token(
            org_id, email, upstream_id,
        )
        await runtime.client_manager.disconnect_user_session(
            upstream_id, email,
        )
        # Caller's MCP clients still list this upstream's tools until
        # the next /tools/list. Push a user-scoped change so they
        # drop them.
        notify_policy_change(deps, user=email)
        return {"status": "disconnected"}

    return router
