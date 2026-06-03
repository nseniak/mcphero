"""Misc admin meta routes — startup-status (1 route)."""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps


class StartupStatusResponse(BaseModel):
    ready: bool
    total: int
    connected: int
    failed: int


def create_meta_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get("/startup-status", response_model=StartupStatusResponse)
    async def startup_status() -> Any:
        if deps.get_startup_status is not None:
            return deps.get_startup_status()
        return StartupStatusResponse(ready=True, total=0, connected=0, failed=0)

    return router
