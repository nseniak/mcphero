"""Dashboard config router — gateway URL + connected/all-users (1 route)."""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import (
    DashboardDeps,
    compose_user_mcp_url,
)


def create_config_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(prefix="/api/config", tags=["dashboard-config"])

    @router.get("/gateway")
    async def gateway_config(
        _email: str = Depends(deps.get_current_user),
    ) -> dict[str, Any]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        # Gateway tokens are user-scoped (global), so the raw connected
        # list spans every org. The dashboard shows per-org "connected
        # users" — intersect with this org's known members. In standalone
        # mode the policy users are the single source of truth, so the
        # intersection collapses to "members who have ever connected".
        global_connected = (
            deps.get_gateway_connected_users()
            if deps.get_gateway_connected_users
            else []
        )
        org_user_emails = set(runtime.policy_engine.config.users.keys())
        connected = (
            sorted(set(global_connected) & org_user_emails)
            if org_user_emails
            else []
        )
        all_emails = sorted(org_user_emails | set(connected))
        org_slug = ""
        if deps.is_cloud_mode and deps.org_repo is not None:
            org = await deps.org_repo.get_organization(org_id)
            if org is not None:
                org_slug = org.slug
        mcp_url = compose_user_mcp_url(
            server_url=deps.gateway_url,
            is_cloud_mode=deps.is_cloud_mode,
            org_slug=org_slug,
        )
        return {
            "url": mcp_url,
            "connected_users": connected,
            "all_users": all_emails,
        }

    return router
