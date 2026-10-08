"""Admin route for revoking a user's gateway tokens (1 route).

Runs the teammate action ``UserAdminService.revoke_gateway_sign_in``; a
refusal becomes an HTTP error through the app-level
``AdminActionRefused`` handler.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends

from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def create_gateway_admin_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.delete("/gateway/users/{email:path}")
    async def disconnect_gateway_user(
        email: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        outcome = await deps.user_admin.revoke_gateway_sign_in(
            current_org_id.get(), email, actor=admin_email,
        )
        logger.info(
            "dashboard.api.admin.gateway_tokens.revoked",
            admin_email=admin_email,
            target_email=email,
            tokens_removed=outcome.tokens_revoked,
            sessions_terminated=outcome.sessions_closed,
        )
        return {
            "status": "ok",
            "detail": f"Revoked {outcome.tokens_revoked} tokens for {email}",
        }

    return router
