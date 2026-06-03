"""Audit search + filter-values router (2 routes)."""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends

from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import (
    DashboardDeps,
    resolve_plan_limits,
)
from mcpolis.entrypoints.routes.dashboard._models import AuditSearchResponse


def create_audit_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get("/audit", response_model=AuditSearchResponse)
    async def search_audit(
        user_id: str = "",
        mcp_id: str = "",
        tool: str = "",
        action: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> AuditSearchResponse:
        org_id = current_org_id.get()
        plan_limits = await resolve_plan_limits(deps, org_id)
        # Plan-driven retention cap: filter at read time so a Free
        # org can never read past 30 days even if the global TTL has
        # not yet purged older rows.
        since = datetime.now(UTC) - timedelta(
            days=plan_limits.audit_retention_days,
        )
        entries = await deps.audit_repo.search(
            org_id,
            user_id=user_id or None,
            mcp_id=mcp_id or None,
            tool=tool or None,
            action=[a for a in action.split(",") if a] or None,
            limit=limit,
            offset=offset,
            since_iso=since.isoformat(),
        )
        return AuditSearchResponse(entries=entries, count=len(entries))

    @router.get("/audit/filters")
    async def audit_filters() -> dict[str, list[str]]:
        return await deps.audit_repo.get_filter_values(current_org_id.get())

    return router
