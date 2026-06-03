# pyright: reportUnusedFunction=false
"""Organization management REST API (cloud mode).

All endpoints live under ``/api/orgs``. They are available in both
standalone and cloud modes for API uniformity, but in standalone mode
creation and invite endpoints return 400 — the ``FileOrganizationRepository``
raises and the service layer surfaces that as a client error.

Security:
- Every endpoint is authenticated via ``get_current_user``.
- Admin endpoints are additionally gated by membership role for the
  target org (not by the global ``require_admin`` dependency, which
  only covers the current session's org). Cross-org admin cannot be
  inferred from the session cookie.
- ``POST /api/orgs/{slug}/invites`` and ``DELETE`` both return 200/204
  regardless of whether the invited email is a known user — this
  avoids email enumeration.
- Unknown slug lookups return **401** (not 404) so an attacker cannot
  distinguish "doesn't exist" from "not a member". See the
  ``test_slug_enumeration_returns_401`` test in Phase 3 for coverage.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.domain.model.upstream import TransportType
from mcpolis.domain.ports.organization_repository import Organization
from mcpolis.domain.services.org_service import (
    OrgNotFoundError,
    OrgService,
    SlugValidationError,
    validate_slug as validate_slug_format,
)

# Callback fired after a new org is created. Receives the newly-created
# org so wiring outside the routes layer (slug-cache invalidation,
# policy-event-listener spawn, display-name caching, …) can react.
# Registered by ``create_app`` — the routes layer stays free of those
# concerns.
OrgCreatedHook = Callable[[Organization], None]
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.routes.dashboard_auth import (
    COOKIE_NAME,
    SESSION_TTL,
    build_session_cookie,
    get_session_payload,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class CreateOrgRequest(BaseModel):
    slug: str
    display_name: str
    created_via: Literal["signup", "manage_page"] = "manage_page"


class OrgSummary(BaseModel):
    slug: str
    display_name: str
    role: str
    is_admin: bool
    plan: str = "free"
    member_count: int = 0
    http_upstream_count: int = 0
    stdio_upstream_count: int = 0


class ListOrgsResponse(BaseModel):
    orgs: list[OrgSummary]
    current_slug: str | None



def create_org_router(
    settings: Settings,
    org_service: OrgService,
    get_current_user: Callable[..., str],
    upstream_config_store: UpstreamConfigStore,
    on_org_created: OrgCreatedHook | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/orgs", tags=["orgs"])

    async def _require_member_role(org_id: str, email: str) -> str:
        role = await org_service.get_user_role(org_id, email)
        if role is None:
            # Anti-enumeration: same 401 as "slug doesn't exist".
            raise HTTPException(
                status_code=401, detail="Not authorized for this org"
            )
        return role

    async def _resolve_slug_or_401(slug: str) -> str:
        try:
            org = await org_service.resolve_slug(slug)
        except OrgNotFoundError:
            raise HTTPException(
                status_code=401, detail="Not authorized for this org"
            )
        return org.id

    @router.get("/check-slug/{slug}")
    async def check_slug(
        slug: str,
        _email: str = Depends(get_current_user),
    ) -> dict[str, object]:
        """Check if a slug is valid and available."""
        try:
            validate_slug_format(slug)
        except SlugValidationError as e:
            return {"available": False, "reason": str(e)}
        try:
            existing = await org_service.resolve_slug(slug)
        except OrgNotFoundError:
            existing = None
        if existing is not None:
            return {"available": False, "reason": "This name is already taken"}
        return {"available": True, "reason": None}

    @router.post("", response_model=OrgSummary)
    async def create_org(
        body: CreateOrgRequest,
        email: str = Depends(get_current_user),
    ) -> OrgSummary:
        # Captured before creation so the just-created org doesn't count.
        existing_orgs = await org_service.list_user_orgs(email)
        is_first_org_for_user = len(existing_orgs) == 0
        try:
            org = await org_service.create_organization(
                slug=body.slug,
                display_name=body.display_name,
                creator_email=email,
            )
        except SlugValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ValueError as e:
            # Covers slug collision and "not supported in standalone".
            raise HTTPException(status_code=400, detail=str(e))
        if on_org_created is not None:
            # Notify listeners (e.g. the OrgContextMiddleware cache so
            # the new slug is immediately resolvable, and the
            # policy-event listener so events for the new org are
            # actually consumed).
            try:
                on_org_created(org)
            except Exception:
                logger.exception(
                    "org.route.created_hook.failed",
                    org_id=org.id,
                    org_slug=org.slug,
                )
        get_analytics().track_async(
            email,
            "org_created",
            {
                "org_slug": org.slug,
                "created_via": body.created_via,
                "is_first_org_for_user": is_first_org_for_user,
            },
        )
        admin_role = await org_service.get_admin_role_name(org.id)
        return OrgSummary(
            slug=org.slug, display_name=org.display_name, role=admin_role,
            is_admin=True,
            member_count=1,
        )

    @router.get("", response_model=ListOrgsResponse)
    async def list_orgs(
        email: str = Depends(get_current_user),
        mcpolis_session: str | None = Cookie(default=None),
    ) -> ListOrgsResponse:
        orgs = await org_service.list_user_orgs(email)
        summaries: list[OrgSummary] = []
        for org in orgs:
            role = await org_service.get_user_role(org.id, email) or ""
            is_admin = await org_service.is_admin(org.id, email)
            members = await org_service.list_members(org.id)
            upstreams = await upstream_config_store.get_all(org.id)
            http_count = sum(
                1 for u in upstreams
                if u.transport == TransportType.streamable_http
            )
            stdio_count = sum(
                1 for u in upstreams if u.transport == TransportType.stdio
            )
            summaries.append(
                OrgSummary(
                    slug=org.slug, display_name=org.display_name, role=role,
                    is_admin=is_admin,
                    plan=org.subscription.plan.value,
                    member_count=len(members),
                    http_upstream_count=http_count,
                    stdio_upstream_count=stdio_count,
                ),
            )
        payload = get_session_payload(settings, mcpolis_session)
        current_slug: str | None = None
        if payload is not None:
            candidate = payload.get("org_slug")
            if isinstance(candidate, str) and candidate:
                current_slug = candidate
        return ListOrgsResponse(orgs=summaries, current_slug=current_slug)

    @router.post("/{slug}/switch")
    async def switch_org(
        slug: str,
        email: str = Depends(get_current_user),
    ) -> Response:
        """Rotate the session cookie to point at a different org.

        Refuses to switch if the caller is not a member of the target
        org. Anti-enumeration: both "doesn't exist" and "not a member"
        return the same 401.
        """
        org_id = await _resolve_slug_or_401(slug)
        await _require_member_role(org_id, email)
        cookie_value = build_session_cookie(
            settings, email=email, org_slug=slug,
        )
        response = Response(status_code=204)
        response.set_cookie(
            COOKIE_NAME,
            cookie_value,
            httponly=True,
            samesite="lax",
            max_age=SESSION_TTL,
            path="/",
        )
        return response

    @router.get("/{slug}/public")
    async def public_org_info(slug: str) -> dict[str, object]:
        """Public org metadata for the invite-link landing page.

        No auth required. Returns ``{exists, display_name}`` so the
        Join page can show "Join Acme" instead of "Join acme-corp"
        and render an "invite link looks broken" state when the slug
        doesn't resolve.

        This is the **one** route in this file that deliberately
        breaks the anti-enumeration property documented at the top:
        an unauthenticated caller can confirm whether a slug exists
        and learn its display name. The trade-off is accepted because:

        * The slug is already in the join URL the inviter shares
          publicly with their team.
        * Org existence is already inferable today via the sign-in
          flow — a non-member completing OAuth gets ``not_a_member``,
          which confirms the org exists.
        * Showing the workspace name on an invite landing is SaaS
          norm (Slack, Linear, Notion, …); the alternative is a
          generic "Join acme-corp" that the recipient can't recognize.

        Membership data and any per-user information are *not*
        exposed. If abuse becomes a concern, add a rate limiter
        keyed on client IP — the existing ``RateLimiter`` adapter
        is already wired into the app.
        """
        try:
            org = await org_service.resolve_slug(slug)
        except OrgNotFoundError:
            return {"exists": False, "display_name": None}
        return {"exists": True, "display_name": org.display_name}

    @router.get("/{slug}/info")
    async def org_info(
        slug: str,
        email: str = Depends(get_current_user),
    ) -> dict[str, object]:
        """Get org info including member count (for delete confirmation)."""
        org_id = await _resolve_slug_or_401(slug)
        await _require_member_role(org_id, email)
        members = await org_service.list_members(org_id)
        org = await org_service.resolve_slug(slug)
        return {
            "slug": org.slug,
            "display_name": org.display_name,
            "member_count": len(members),
        }

    @router.delete("/{slug}")
    async def delete_org(
        slug: str,
        email: str = Depends(get_current_user),
    ) -> Response:
        """Delete an organization. Only admins of the org can do this."""
        try:
            org = await org_service.resolve_slug(slug)
        except OrgNotFoundError:
            raise HTTPException(
                status_code=401, detail="Not authorized for this org"
            )
        await _require_member_role(org.id, email)
        if not await org_service.is_admin(org.id, email):
            raise HTTPException(
                status_code=403, detail="Only admins can delete an organization"
            )
        await org_service.delete_organization(org.id)
        if on_org_created is not None:
            # Reuse the hook for slug-cache invalidation on delete.
            # Passing the deleted org keeps the callback signature
            # uniform; the slug-cache clears wholesale so the specific
            # id doesn't matter here.
            on_org_created(org)
        get_analytics().track_async(email, "org_deleted", {"org_slug": slug})
        return Response(status_code=204)

    return router
