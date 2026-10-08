"""Invitation router (2 routes): the invited person accepts or declines.

- ``POST /api/invitations/{slug}/accept`` — the Join button: the
  invitation becomes a membership.
- ``POST /api/invitations/{slug}/decline`` — the invitation is deleted.

Only the signed-in person's own invitation is ever touched, and only by
them. An unknown slug and a missing invitation answer the same 404, so
these routes don't tell anyone which orgs exist. A refusal becomes an
HTTP error through the app-level ``AdminActionRefused`` handler.

The pending invitations themselves are listed in ``/api/auth/me``.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from fastapi import APIRouter, Depends

from mcpolis.domain.services.admin_actions import NotFound
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps


def create_invitations_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(prefix="/api/invitations", tags=["invitations"])
    # The invited person is not a member yet: only their sign-in counts.
    signed_in = deps.get_session_user or deps.get_current_user

    async def org_id_for(slug: str, message: str) -> str:
        org = (
            await deps.org_repo.get_by_slug(slug)
            if deps.org_repo is not None else None
        )
        if org is None:
            raise NotFound(message)
        return org.id

    @router.post("/{slug}/accept")
    async def accept_invitation(
        slug: str,
        email: str = Depends(signed_in),
    ) -> dict[str, str]:
        org_id = await org_id_for(slug, "No invitation to accept")
        await deps.user_admin.accept_invitation(org_id, email)
        return {"status": "joined", "slug": slug}

    @router.post("/{slug}/decline")
    async def decline_invitation(
        slug: str,
        email: str = Depends(signed_in),
    ) -> dict[str, str]:
        org_id = await org_id_for(slug, "No invitation to decline")
        await deps.user_admin.decline_invitation(org_id, email)
        return {"status": "declined", "slug": slug}

    return router
