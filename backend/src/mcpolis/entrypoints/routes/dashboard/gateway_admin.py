"""Admin route for revoking a user's gateway tokens (1 route)."""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

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
        if deps.revoke_gateway_user is None:
            raise HTTPException(400, "OAuth is not enabled")
        removed = deps.revoke_gateway_user(email)
        if removed == 0:
            raise HTTPException(404, f"No tokens found for {email}")
        logger.info(
            "dashboard.api.admin.gateway_tokens.revoked",
            admin_email=admin_email,
            target_email=email,
            tokens_removed=removed,
        )
        return {
            "status": "ok",
            "detail": f"Revoked {removed} tokens for {email}",
        }

    return router
