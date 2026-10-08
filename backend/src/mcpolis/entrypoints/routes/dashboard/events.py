"""SSE event-stream router (1 route).

The dashboard tab opens this once on load and keeps it open for the
session — the gateway publishes per-org events here when policy
changes, OAuth callbacks land, etc. Anonymous callers get 401 via
``Depends(get_current_user)``.

Every event published to an org reaches every open tab of that org, so
the stream itself decides what a tab may see:

- An audit row (``audit_entry``) goes only to the org's admins (and MCP
  Hero operators browsing the org): the Audit page is admin-only.
- The stream ends as soon as its person is no longer a member of the
  org: a removed member's open tab stops receiving the org's events.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps

AUDIT_ENTRY_EVENT = "audit_entry"


async def dashboard_event_frames(
    bus: EventStream,
    runtime_manager: OrgRuntimeManager,
    org_id: str,
    email: str,
    *,
    is_superadmin: bool,
) -> AsyncIterator[str]:
    """The SSE frames one dashboard tab of ``email`` gets for ``org_id``.

    Checked against the org's running policy at every event and every
    keepalive (at most 15 s apart), so a removal or a demotion takes
    effect on an open tab without waiting for it to reconnect.
    """
    events = bus.subscribe(org_id, email)
    try:
        async for event in events:
            policy = (await runtime_manager.get(org_id)).policy_engine
            if not is_superadmin and not policy.is_member(email):
                return
            if event is None:
                yield ": keepalive\n\n"
                continue
            if event.type == AUDIT_ENTRY_EVENT and not (
                is_superadmin or policy.is_admin(email)
            ):
                continue
            yield (
                f"event: {event.type}\n"
                f"data: {event.model_dump_json()}\n\n"
            )
    finally:
        # Unsubscribe now, not whenever the subscription is collected.
        if isinstance(events, AsyncGenerator):
            await events.aclose()


def create_events_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["events"])

    @router.get("/events")
    async def event_stream(
        email: str = Depends(deps.get_current_user),
    ) -> StreamingResponse:
        if deps.event_bus is None:
            raise HTTPException(501, "Event bus not available")
        return StreamingResponse(
            dashboard_event_frames(
                deps.event_bus,
                deps.runtime_manager,
                current_org_id.get(),
                email,
                is_superadmin=email in deps.superadmin_emails,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
