# pyright: reportUnusedFunction=false
"""Superadmin dashboard HTTP routes.

These endpoints back the ``/superadmin/*`` cross-org browse UI. They
are gated by membership in ``MCPOLIS_SUPERADMIN_EMAILS`` — the same
allowlist used by ``/admin-mcp/system`` and ``/api/debug/*``.

Unlike ``debug_routes`` and ``operator_routes`` (which use 404 to hide
their existence from non-operators), the superadmin dashboard is a
deliberately named feature surfaced via a sidebar link, so the gate
returns **403** when an authenticated non-superadmin hits it. Anonymous
callers still get 401 from the underlying ``get_current_user``
dependency.

Phase 1 surface: a single ``GET /api/superadmin/overview`` that powers
the landing page tiles + system status strip. Subsequent phases (orgs,
users, upstreams, audit, auth-health, system, soft actions) layer on
top of the same router and the same ``require_superadmin`` dep.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.ports import ADMIN_USER_ID
from mcpolis.domain.ports.audit_repository import AuditRepository
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.org_service import OrgService
from mcpolis.entrypoints.config import Settings

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class OverviewCounts(BaseModel):
    orgs: int
    users: int
    upstreams: int
    upstreams_connected: int
    runtimes_loaded: int


class OverviewSystemStatus(BaseModel):
    mode: Literal["standalone", "cloud"]
    mixpanel_enabled: bool
    sentry_enabled: bool


class OverviewResponse(BaseModel):
    counts: OverviewCounts
    system: OverviewSystemStatus


# --- Orgs ---


class OrgListRow(BaseModel):
    id: str
    slug: str
    display_name: str
    created_at: str
    created_by_email: str | None
    member_count: int
    upstream_count: int
    upstream_connected: int
    runtime_loaded: bool
    plan: str


class OrgListResponse(BaseModel):
    orgs: list[OrgListRow]


class SubscriptionUpdateResponse(BaseModel):
    org_id: str
    plan: str


class UpdateSubscriptionRequest(BaseModel):
    plan: PlanName


# --- Users ---


class UserOrgMembership(BaseModel):
    org_id: str
    org_slug: str
    org_display_name: str
    role: str
    is_admin: bool
    joined_at: str


class UserListRow(BaseModel):
    email: str
    org_count: int
    is_superadmin: bool
    roles: list[str]


class UserListResponse(BaseModel):
    users: list[UserListRow]


class UserDetailResponse(BaseModel):
    email: str
    is_superadmin: bool
    memberships: list[UserOrgMembership]


# --- Upstreams (cross-org) ---


class UpstreamListRow(BaseModel):
    org_id: str
    org_slug: str
    org_display_name: str
    upstream_id: str
    display_name: str
    transport: str
    auth_mode: str
    connected: bool


class UpstreamListResponse(BaseModel):
    upstreams: list[UpstreamListRow]
    runtimes_loaded: int
    runtimes_total: int


# --- Audit (cross-org) ---


class AuditSearchResponse(BaseModel):
    entries: list[dict[str, Any]]
    count: int


class AuditAggregatesTopRow(BaseModel):
    key: str
    count: int


class AuditAggregatesResponse(BaseModel):
    sample_size: int
    top_tools: list[AuditAggregatesTopRow]
    top_orgs: list[AuditAggregatesTopRow]
    top_deny_rules: list[AuditAggregatesTopRow]
    latency_p50_ms: float | None
    latency_p95_ms: float | None


# --- Auth health ---


class AuthHealthRow(BaseModel):
    org_id: str
    org_slug: str
    upstream_id: str
    user_id: str
    is_admin_token: bool
    expires_at: str | None
    refresh_failures: int
    last_failure_at: str | None
    expired: bool
    expiring_soon: bool


class AuthHealthConnectionError(BaseModel):
    org_id: str
    org_slug: str
    upstream_id: str
    error: str


class AuthHealthResponse(BaseModel):
    runtimes_loaded: int
    runtimes_total: int
    total_tokens: int
    expired: int
    expiring_soon: int
    failed_refresh: int
    rows: list[AuthHealthRow]
    connection_errors: list[AuthHealthConnectionError]


# --- System ---


class SystemBackendInfo(BaseModel):
    mode: Literal["standalone", "cloud"]
    release: str
    sentry_dsn_set: bool
    sentry_environment: str
    mixpanel_token_set: bool
    mixpanel_api_host: str
    mongo_uri_set: bool
    mongo_db_name: str
    redis_url_set: bool
    encryption_key_set: bool
    encryption_key_fingerprint: str
    test_mode: bool
    allow_stdio_mcp: bool


class SystemResponse(BaseModel):
    backend: SystemBackendInfo


# --- Soft actions ---


class SessionsRevokedResponse(BaseModel):
    email: str
    gateway_tokens_revoked: int
    upstream_sessions_terminated: int
    orgs_touched: int


class ReauthResponse(BaseModel):
    email: str
    org_id: str
    upstream_id: str
    cleared: bool


def create_superadmin_router(
    settings: Settings,
    org_repo: OrganizationRepository,
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService,
    audit_repo: AuditRepository,
    connection_store: ConnectionStore,
    revoke_gateway_user: Callable[[str], int],
    terminate_gateway_sessions: Callable[[str, str], int],
    get_current_user: Callable[..., str],
    superadmin_emails: set[str],
) -> APIRouter:
    """Build the ``/api/superadmin/*`` router.

    All routes require an authenticated user whose email is in
    ``superadmin_emails`` (sourced from
    ``MCPOLIS_SUPERADMIN_EMAILS``). Non-superadmins get 403; anonymous
    callers get 401 from ``get_current_user``.
    """
    router = APIRouter(prefix="/api/superadmin", tags=["superadmin"])

    async def require_superadmin(
        email: str = Depends(get_current_user),
    ) -> str:
        if email not in superadmin_emails:
            raise HTTPException(
                status_code=403, detail="Superadmin role required",
            )
        return email

    @router.get("/overview", response_model=OverviewResponse)
    async def overview(
        _email: str = Depends(require_superadmin),
    ) -> OverviewResponse:
        """Cross-org top-line counts + system status strip.

        Uses cached runtimes for upstream/connected counts so a slow
        org (Mongo round-trip per upstream) can't stall the page —
        any orgs not yet loaded just contribute 0 to the upstream
        totals. The ``runtimes_loaded`` field surfaces this so the
        UI can flag "X of Y orgs cold-loaded" if needed later.
        """
        orgs = await org_repo.list_organizations()
        runtimes = runtime_manager.all_runtimes

        upstream_count = 0
        connected_count = 0
        for runtime in runtimes.values():
            upstream_count += len(runtime.live_upstreams())
            connected_count += len(
                runtime.client_manager.ready_upstream_ids,
            )

        # Distinct user count across all orgs. Each membership row is
        # one (org, email); the same email in multiple orgs counts
        # once, matching what the user would expect on a "Users"
        # tile.
        emails: set[str] = set()
        for org in orgs:
            for membership in await org_repo.list_memberships(org.id):
                emails.add(membership.email)

        return OverviewResponse(
            counts=OverviewCounts(
                orgs=len(orgs),
                users=len(emails),
                upstreams=upstream_count,
                upstreams_connected=connected_count,
                runtimes_loaded=len(runtimes),
            ),
            system=OverviewSystemStatus(
                mode=settings.mode,
                mixpanel_enabled=bool(settings.mixpanel_token),
                sentry_enabled=bool(settings.sentry_dsn),
            ),
        )

    @router.get("/orgs", response_model=OrgListResponse)
    async def list_orgs(
        _email: str = Depends(require_superadmin),
    ) -> OrgListResponse:
        """List every organization with summary counts.

        Upstream / connected counts come from the cached runtime — orgs
        whose runtime hasn't been spun up yet contribute 0 and surface
        ``runtime_loaded=false`` so the UI can disambiguate "no
        upstreams" from "not loaded yet".
        """
        orgs = await org_repo.list_organizations()
        rows: list[OrgListRow] = []
        for org in orgs:
            members = await org_repo.list_memberships(org.id)
            runtime = runtime_manager.get_cached(org.id)
            if runtime is not None:
                upstream_count = len(runtime.live_upstreams())
                connected = len(runtime.client_manager.ready_upstream_ids)
                runtime_loaded = True
            else:
                upstream_count = 0
                connected = 0
                runtime_loaded = False
            rows.append(
                OrgListRow(
                    id=org.id,
                    slug=org.slug,
                    display_name=org.display_name,
                    created_at=org.created_at.isoformat(),
                    created_by_email=org.created_by_email,
                    member_count=len(members),
                    upstream_count=upstream_count,
                    upstream_connected=connected,
                    runtime_loaded=runtime_loaded,
                    plan=org.subscription.plan.value,
                ),
            )
        return OrgListResponse(orgs=rows)

    @router.patch(
        "/orgs/{org_id}/subscription",
        response_model=SubscriptionUpdateResponse,
    )
    async def update_org_subscription(
        org_id: str,
        body: UpdateSubscriptionRequest,
        email: str = Depends(require_superadmin),
    ) -> SubscriptionUpdateResponse:
        """Flip an org between ``free`` and ``team``.

        Temporary stand-in for billing — once Stripe integration lands
        the plan flip should follow a successful checkout/cancellation
        webhook. Until then this endpoint is the only knob.
        """
        org = await org_repo.get_organization(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        previous_plan = org.subscription.plan.value
        await org_repo.update_subscription(
            org_id, Subscription(plan=body.plan),
        )
        get_analytics().track_async(
            email,
            "superadmin_plan_changed",
            {
                "org_id": org_id,
                "from_plan": previous_plan,
                "to_plan": body.plan.value,
            },
        )
        return SubscriptionUpdateResponse(
            org_id=org_id, plan=body.plan.value,
        )

    @router.get("/users", response_model=UserListResponse)
    async def list_users(
        _email: str = Depends(require_superadmin),
    ) -> UserListResponse:
        """Cross-org user list.

        Aggregates memberships across every org. ``roles`` is the
        deduplicated set of role names the user holds across all
        their orgs (e.g. ["admin", "user"] for a user who is admin
        somewhere and a regular user elsewhere).
        """
        orgs = await org_repo.list_organizations()
        per_email_orgs: dict[str, list[str]] = {}
        per_email_roles: dict[str, set[str]] = {}
        for org in orgs:
            for membership in await org_repo.list_memberships(org.id):
                per_email_orgs.setdefault(membership.email, []).append(org.id)
                per_email_roles.setdefault(membership.email, set()).add(
                    membership.role,
                )

        rows = [
            UserListRow(
                email=email,
                org_count=len(org_ids),
                is_superadmin=email in superadmin_emails,
                roles=sorted(per_email_roles.get(email, set())),
            )
            for email, org_ids in sorted(per_email_orgs.items())
        ]
        return UserListResponse(users=rows)

    @router.get("/users/{email}", response_model=UserDetailResponse)
    async def get_user(
        email: str,
        _caller: str = Depends(require_superadmin),
    ) -> UserDetailResponse:
        memberships = await org_repo.get_memberships_for_email(email)
        if not memberships and email not in superadmin_emails:
            # Surface a clean 404 rather than returning an empty
            # detail page; the UI's "user not found" branch is then
            # easy to render.
            raise HTTPException(status_code=404, detail="User not found")

        rows: list[UserOrgMembership] = []
        for membership in memberships:
            org = await org_repo.get_organization(membership.org_id)
            # Defensive: a membership row with a deleted org should
            # not crash the page; show a placeholder slug + name
            # instead so the operator can spot the inconsistency.
            slug = org.slug if org else "<deleted>"
            display_name = (
                org.display_name if org else f"<deleted org {membership.org_id}>"
            )
            # Reconcile against policy config — the membership row
            # may say "user" while the policy config says the user
            # has an admin-flagged role if a membership lagged a
            # policy edit. Trust the policy config (what the gateway
            # actually enforces).
            policy_role = await org_service.get_user_role(
                membership.org_id, email,
            )
            is_admin = await org_service.is_admin(membership.org_id, email)
            rows.append(
                UserOrgMembership(
                    org_id=membership.org_id,
                    org_slug=slug,
                    org_display_name=display_name,
                    role=policy_role or membership.role,
                    is_admin=is_admin,
                    joined_at=membership.created_at.isoformat(),
                ),
            )

        return UserDetailResponse(
            email=email,
            is_superadmin=email in superadmin_emails,
            memberships=rows,
        )

    @router.get("/upstreams", response_model=UpstreamListResponse)
    async def list_upstreams(
        _email: str = Depends(require_superadmin),
    ) -> UpstreamListResponse:
        """Cross-org upstream list.

        Walks every loaded runtime; orgs whose runtime hasn't been
        spun up since boot contribute zero rows. The response carries
        ``runtimes_loaded`` / ``runtimes_total`` so the UI can warn
        the operator when data is partial.
        """
        orgs = await org_repo.list_organizations()
        slug_by_id = {o.id: o.slug for o in orgs}
        name_by_id = {o.id: o.display_name for o in orgs}

        rows: list[UpstreamListRow] = []
        loaded = 0
        for org in orgs:
            runtime = runtime_manager.get_cached(org.id)
            if runtime is None:
                continue
            loaded += 1
            ready_ids = runtime.client_manager.ready_upstream_ids
            for u in runtime.live_upstreams():
                rows.append(
                    UpstreamListRow(
                        org_id=org.id,
                        org_slug=slug_by_id.get(org.id, org.id),
                        org_display_name=name_by_id.get(org.id, org.id),
                        upstream_id=u.id,
                        display_name=u.display_name,
                        transport=str(u.transport),
                        auth_mode=str(u.auth.mode),
                        connected=u.id in ready_ids,
                    ),
                )

        return UpstreamListResponse(
            upstreams=rows,
            runtimes_loaded=loaded,
            runtimes_total=len(orgs),
        )

    @router.get("/audit", response_model=AuditSearchResponse)
    async def audit_search(
        org_id: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        upstream_id: str | None = Query(default=None),
        tool: str | None = Query(default=None),
        action: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        _email: str = Depends(require_superadmin),
    ) -> AuditSearchResponse:
        """Cross-org audit search.

        ``org_id`` filter is applied client-side after a cross-org
        fetch so we don't need a separate scoped path; same code path
        for "everything" and "this org only".
        """
        actions = [action] if action else None
        entries = await audit_repo.search_cross_org(
            user_id=user_id,
            mcp_id=upstream_id,
            tool=tool,
            action=actions,
            limit=limit,
            offset=offset,
        )
        if org_id:
            entries = [e for e in entries if e.get("org_id") == org_id]
        return AuditSearchResponse(entries=entries, count=len(entries))

    @router.get("/audit/aggregates", response_model=AuditAggregatesResponse)
    async def audit_aggregates(
        org_id: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        upstream_id: str | None = Query(default=None),
        tool: str | None = Query(default=None),
        action: str | None = Query(default=None),
        sample_size: int = Query(default=2000, ge=100, le=10000),
        _email: str = Depends(require_superadmin),
    ) -> AuditAggregatesResponse:
        """Top-N tools / orgs / deny rules + latency percentiles.

        Computed in-process from the most recent ``sample_size``
        matching entries. For Mongo-scale audit logs this is a
        sliding window — fine for a v1 dashboard, swap for a real
        ``$group`` pipeline if it ever proves slow.
        """
        actions = [action] if action else None
        entries = await audit_repo.search_cross_org(
            user_id=user_id,
            mcp_id=upstream_id,
            tool=tool,
            action=actions,
            limit=sample_size,
            offset=0,
        )
        if org_id:
            entries = [e for e in entries if e.get("org_id") == org_id]

        tools_count: dict[str, int] = {}
        orgs_count: dict[str, int] = {}
        deny_rules_count: dict[str, int] = {}
        latencies: list[float] = []
        for e in entries:
            t = e.get("tool")
            if isinstance(t, str) and t:
                tools_count[t] = tools_count.get(t, 0) + 1
            o = e.get("org_id")
            if isinstance(o, str) and o:
                orgs_count[o] = orgs_count.get(o, 0) + 1
            if e.get("policy_decision") == "deny":
                rule = e.get("policy_rule") or "<unspecified>"
                if isinstance(rule, str):
                    deny_rules_count[rule] = deny_rules_count.get(rule, 0) + 1
            lat = e.get("latency_ms")
            if isinstance(lat, (int, float)):
                latencies.append(float(lat))

        def _top(d: dict[str, int]) -> list[AuditAggregatesTopRow]:
            return [
                AuditAggregatesTopRow(key=k, count=v)
                for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:10]
            ]

        latencies.sort()

        def _pct(p: float) -> float | None:
            if not latencies:
                return None
            idx = int(round((p / 100) * (len(latencies) - 1)))
            return latencies[idx]

        return AuditAggregatesResponse(
            sample_size=len(entries),
            top_tools=_top(tools_count),
            top_orgs=_top(orgs_count),
            top_deny_rules=_top(deny_rules_count),
            latency_p50_ms=_pct(50),
            latency_p95_ms=_pct(95),
        )

    @router.get("/auth-health", response_model=AuthHealthResponse)
    async def auth_health(
        _email: str = Depends(require_superadmin),
    ) -> AuthHealthResponse:
        """OAuth/connection health across loaded runtimes.

        Walks ``connection_store.get_all_stored_tokens`` for every
        loaded org and classifies each (user, upstream) row as
        expired / expiring soon (24h) / refresh-failed. Cold orgs
        contribute nothing — surfaces ``runtimes_loaded`` /
        ``runtimes_total`` so the UI can flag when data is partial.
        """
        orgs = await org_repo.list_organizations()
        slug_by_id = {o.id: o.slug for o in orgs}
        runtimes = runtime_manager.all_runtimes
        now = datetime.now(UTC)
        soon_window = now + timedelta(hours=24)

        rows: list[AuthHealthRow] = []
        errors: list[AuthHealthConnectionError] = []
        expired = 0
        expiring_soon = 0
        failed_refresh = 0

        for org_id in runtimes:
            slug = slug_by_id.get(org_id, org_id)
            stored = await connection_store.get_all_stored_tokens(org_id)
            for user_id, upstream_id in stored:
                if user_id == ADMIN_USER_ID:
                    token = await connection_store.get_admin_token(
                        org_id, upstream_id,
                    )
                else:
                    token = await connection_store.get_user_token(
                        org_id, user_id, upstream_id,
                    )
                expires_at = token.expires_at if token else None
                is_expired = bool(
                    expires_at is not None and expires_at <= now,
                )
                is_soon = bool(
                    expires_at is not None
                    and not is_expired
                    and expires_at <= soon_window,
                )
                failures = await connection_store.get_refresh_failures(
                    org_id, upstream_id, user_id,
                )
                fail_count = failures[0] if failures else 0
                fail_at = failures[1].isoformat() if failures else None
                if is_expired:
                    expired += 1
                if is_soon:
                    expiring_soon += 1
                if fail_count > 0:
                    failed_refresh += 1
                rows.append(
                    AuthHealthRow(
                        org_id=org_id,
                        org_slug=slug,
                        upstream_id=upstream_id,
                        user_id=user_id,
                        is_admin_token=user_id == ADMIN_USER_ID,
                        expires_at=expires_at.isoformat() if expires_at else None,
                        refresh_failures=fail_count,
                        last_failure_at=fail_at,
                        expired=is_expired,
                        expiring_soon=is_soon,
                    ),
                )

            # Per-(org, upstream) connection errors (one row per
            # upstream that's currently in error state, regardless of
            # whether the row already shows up under tokens).
            runtime = runtime_manager.get_cached(org_id)
            if runtime is None:
                continue
            for u in runtime.live_upstreams():
                err = await connection_store.get_connection_error(
                    org_id, u.id,
                )
                if err:
                    errors.append(
                        AuthHealthConnectionError(
                            org_id=org_id,
                            org_slug=slug,
                            upstream_id=u.id,
                            error=err,
                        ),
                    )

        # Worst-first ordering: failures by count desc, then expired,
        # then expiring soon.
        rows.sort(
            key=lambda r: (-r.refresh_failures, not r.expired, not r.expiring_soon),
        )

        return AuthHealthResponse(
            runtimes_loaded=len(runtimes),
            runtimes_total=len(orgs),
            total_tokens=len(rows),
            expired=expired,
            expiring_soon=expiring_soon,
            failed_refresh=failed_refresh,
            rows=rows,
            connection_errors=errors,
        )

    @router.post(
        "/users/{email}/sessions/revoke",
        response_model=SessionsRevokedResponse,
    )
    async def revoke_user_sessions(
        email: str,
        caller: str = Depends(require_superadmin),
    ) -> SessionsRevokedResponse:
        """Kick a user out everywhere — soft, reversible.

        Revokes the user's gateway access tokens (forces every MCP
        client to re-auth) and terminates any in-flight gateway
        sessions for them in every org they belong to. Does NOT
        delete OAuth tokens, role assignments, or memberships.
        """
        gateway_revoked = revoke_gateway_user(email)
        memberships = await org_repo.get_memberships_for_email(email)
        org_ids = {m.org_id for m in memberships}
        terminated = 0
        for org_id in org_ids:
            terminated += terminate_gateway_sessions(org_id, email)
        logger.info(
            "superadmin.sessions.revoked",
            actor=caller,
            target=email,
            gateway_tokens=gateway_revoked,
            sessions_terminated=terminated,
            orgs=len(org_ids),
        )
        return SessionsRevokedResponse(
            email=email,
            gateway_tokens_revoked=gateway_revoked,
            upstream_sessions_terminated=terminated,
            orgs_touched=len(org_ids),
        )

    @router.post(
        "/users/{email}/connections/{org_id}/{upstream_id}/reauth",
        response_model=ReauthResponse,
    )
    async def trigger_reauth(
        email: str,
        org_id: str,
        upstream_id: str,
        caller: str = Depends(require_superadmin),
    ) -> ReauthResponse:
        """Clear a stuck OAuth connection so the user re-authenticates.

        Deletes the per-user OAuth token for ``(org, upstream, user)``;
        the next request from that user surfaces "Authentication
        needed" and goes through the OAuth flow. Other tokens for
        the same user in other orgs/upstreams are untouched.
        """
        org = await org_repo.get_organization(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        # delete_user_token returns nothing — call it and trust it
        # to be idempotent (it is in both backends).
        await connection_store.delete_user_token(org_id, email, upstream_id)
        logger.info(
            "superadmin.connection.reauth_triggered",
            actor=caller,
            target=email,
            org_id=org_id,
            upstream_id=upstream_id,
        )
        return ReauthResponse(
            email=email,
            org_id=org_id,
            upstream_id=upstream_id,
            cleared=True,
        )

    @router.get("/system", response_model=SystemResponse)
    async def system(
        _email: str = Depends(require_superadmin),
    ) -> SystemResponse:
        """Backend deployment info — env presence flags only.

        Never returns the secret values themselves, only "set/not set"
        and a stable fingerprint of the encryption key (first 8 hex
        of SHA-256) so the operator can confirm prod and dev share
        (or don't share) the same key without leaking material.
        """
        if settings.encryption_key:
            fingerprint = sha256(
                settings.encryption_key.encode(),
            ).hexdigest()[:8]
        else:
            fingerprint = ""
        return SystemResponse(
            backend=SystemBackendInfo(
                mode=settings.mode,
                release=settings.release,
                sentry_dsn_set=bool(settings.sentry_dsn),
                sentry_environment=settings.sentry_environment,
                mixpanel_token_set=bool(settings.mixpanel_token),
                mixpanel_api_host=settings.mixpanel_api_host,
                mongo_uri_set=bool(settings.mongo_uri),
                mongo_db_name=settings.mongo_db_name,
                redis_url_set=bool(settings.redis_url),
                encryption_key_set=bool(settings.encryption_key),
                encryption_key_fingerprint=fingerprint,
                test_mode=settings.test_mode,
                allow_stdio_mcp=settings.allow_stdio_mcp,
            ),
        )

    return router
