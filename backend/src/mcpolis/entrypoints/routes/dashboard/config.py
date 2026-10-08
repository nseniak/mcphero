"""Dashboard config router — gateway URL + connected/all-users (1 route)."""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from mcpolis.domain.model.email_address import email_key
from mcpolis.domain.services.user_admin_service import active_member_emails
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
        # users": intersect with this org's MEMBERS. A pending invitation
        # is not one, so inviting an address can't reveal whether that
        # person uses MCP Hero through another org.
        global_connected = (
            deps.get_gateway_connected_users()
            if deps.get_gateway_connected_users
            else []
        )
        members = await active_member_emails(
            deps.org_repo, org_id, runtime.policy_engine.config,
        )
        # A member invited as ``Bob@Acme.com`` signs in as
        # ``bob@acme.com``: letter case is ignored.
        connected_keys = {email_key(email) for email in global_connected}
        connected = sorted(e for e in members if email_key(e) in connected_keys)
        all_emails = sorted(members)
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
