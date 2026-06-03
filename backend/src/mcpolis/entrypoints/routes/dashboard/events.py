"""SSE event-stream router (1 route).

The dashboard tab opens this once on load and keeps it open for the
session — the gateway publishes per-org events here when policy
changes, OAuth callbacks land, etc. Anonymous callers get 401 via
``Depends(get_current_user)``.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps


def create_events_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["events"])

    @router.get("/events")
    async def event_stream(
        email: str = Depends(deps.get_current_user),
    ) -> StreamingResponse:
        if deps.event_bus is None:
            raise HTTPException(501, "Event bus not available")
        bus = deps.event_bus

        async def generate() -> AsyncIterator[str]:
            async for event in bus.subscribe(current_org_id.get(), email):
                if event is None:
                    yield ": keepalive\n\n"
                else:
                    yield (
                        f"event: {event.type}\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
