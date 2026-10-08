# pyright: reportUnusedFunction=false
"""Dashboard authentication: Google OAuth browser flow + signed session cookie.

Separate from the MCP SDK OAuth machinery — this is a standard browser-based
Google sign-in that sets an HttpOnly cookie for the dashboard SPA.
"""
from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeGuard
from urllib.parse import urlencode

import structlog
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mcpolis.adapters.auth.hmac_token import sign_token, verify_token
from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    ACCESS_TOKEN_TTL,
    McpGatewayOAuthProvider,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.ports.dashboard_oauth_provider import DashboardOAuthProvider
from mcpolis.domain.model.settings import UserDefinition
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_org_slug,
)
from mcpolis.domain.ports.session_revocation import SessionRevocationStore
from mcpolis.domain.services.audit_actions import acting_as_operator
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.org_service import OrgService
from mcpolis.entrypoints.config import Settings

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

COOKIE_NAME = "mcpolis_session"
SESSION_TTL = 86400 * 7  # 7 days

# In-memory pending state for dashboard login.
# Maps state token → (created_at, join_slug_or_none).
_pending_logins: dict[str, tuple[float, str | None]] = {}


class OrgMembership(BaseModel):
    """Compact org summary returned in ``/api/auth/me``."""

    slug: str
    display_name: str
    role: str
    is_admin: bool
    plan: str = "free"


class CurrentOrgInfo(BaseModel):
    slug: str
    display_name: str
    role: str
    is_admin: bool
    plan: str = "free"


class InvitationInfo(BaseModel):
    """An org's invitation the signed-in person has not accepted yet."""

    slug: str
    display_name: str
    role: str


class AuthUserInfo(BaseModel):
    email: str
    roles: list[str]
    is_admin: bool
    is_superadmin: bool = False
    orgs: list[OrgMembership] = []
    current_org: CurrentOrgInfo | None = None
    invitations: list[InvitationInfo] = []


class AuthStatus(BaseModel):
    has_users: bool


def get_signing_key(settings: Settings) -> bytes:
    """Derive the signing key from settings (shared by cookies and OAuth state tokens).

    Uses HKDF(SHA-256) with an ``info`` label, so the same master secret
    can safely produce distinct keys for different purposes (session
    cookies, OAuth state tokens, encryption-at-rest) without key reuse
    across contexts.

    Source-secret policy: ``MCPOLIS_SESSION_SECRET`` only. There is NO
    fallback to ``google_client_secret`` — OAuth client secrets get
    pasted around (local .env, CI, sometimes screenshotted) and reusing
    one for session signing means a leaked client secret forges admin
    cookies. Standalone dev without an explicit secret uses a literal
    dev default; cloud mode rejects that at startup
    (see ``validate_startup_secrets``).
    """
    secret = settings.session_secret or "mcpolis-dev-secret"
    # ``info`` is a stable key-derivation salt. Rotating it invalidates
    # every signed cookie already issued under the old label, logging
    # every active user out. Treat it like a schema version.
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=None,
        info=b"mcpolis-session",
    ).derive(secret.encode())


def _sign_cookie(payload: dict[str, Any], key: bytes) -> str:
    """Create a signed cookie value: base64(payload).base64(hmac)."""
    return sign_token(payload, key)


def _verify_cookie(cookie_value: str, key: bytes) -> dict[str, Any] | None:
    """Verify and decode a signed cookie. Returns None if invalid.

    A session cookie without an end (``exp``) is refused: the token
    format would accept it for ever, and the server never mints one.
    """
    result: dict[str, Any] | None = verify_token(cookie_value, key)
    if result is None or not _is_time(result.get("exp")):
        return None
    return result


def _get_current_user_from_cookie(
    settings: Settings,
    cookie_value: str | None,
) -> str | None:
    """Extract email from a valid session cookie. Returns None if invalid."""
    if not cookie_value:
        return None
    key = get_signing_key(settings)
    payload = _verify_cookie(cookie_value, key)
    if payload is None:
        return None
    return payload.get("email")  # type: ignore[no-any-return]


def get_session_payload(
    settings: Settings, cookie_value: str | None,
) -> dict[str, Any] | None:
    """Return the decoded cookie payload (including org_slug) or None.

    Exported (no leading underscore) so the org-routes module can read
    the ``org_slug`` field without having to re-decode the cookie.
    """
    if not cookie_value:
        return None
    key = get_signing_key(settings)
    return _verify_cookie(cookie_value, key)


def build_session_cookie(
    settings: Settings,
    email: str,
    org_slug: str,
    *,
    signed_in_at: float | None = None,
    expires_at: float | None = None,
    sign_in_id: str | None = None,
) -> str:
    """Create a session cookie payload.

    ``org_slug`` may be empty when the user has just signed in and does
    not yet belong to any org — the frontend reads that as "redirect to
    signup". In standalone mode the slug is always ``default``.

    Each cookie carries a unique ``jti`` (this cookie) and a ``sid``
    (the Google sign-in it belongs to, kept across org switches), so
    ``/logout`` can end exactly this sign-in, with every cookie an org
    switch made from it, without affecting the user's other devices.

    ``signed_in_at`` / ``expires_at`` / ``sign_in_id`` default to a
    fresh sign-in (now, now + ``SESSION_TTL``, a new id). Only a Google
    sign-in may use the defaults:
    a cookie re-issued from an existing session must pass that
    session's own times (``build_switched_session_cookie``), or the
    re-issue would extend the sign-in without Google.
    """
    key = get_signing_key(settings)
    iat = time.time() if signed_in_at is None else signed_in_at
    exp = iat + SESSION_TTL if expires_at is None else expires_at
    return _sign_cookie(
        {
            "email": email,
            "org_slug": org_slug,
            "jti": secrets.token_urlsafe(16),
            "sid": sign_in_id or secrets.token_urlsafe(16),
            "iat": iat,
            "exp": exp,
        },
        key,
    )


def build_switched_session_cookie(
    settings: Settings, payload: dict[str, Any], org_slug: str,
) -> str | None:
    """The cookie for the same sign-in, pointing at ``org_slug``.

    Keeps the sign-in id, the sign-in time and the end of the original
    Google sign-in (never later than sign-in + ``SESSION_TTL``), so
    switching org can't make a sign-in last longer and ``/logout`` of
    any cookie of the sign-in ends them all. ``None`` when ``payload``
    lacks a usable email, sign-in time, end or id.
    """
    email = payload.get("email")
    iat = payload.get("iat")
    exp = payload.get("exp")
    sign_in_id = _sign_in_id(payload)
    if (
        not isinstance(email, str)
        or not _is_time(iat)
        or not _is_time(exp)
        or sign_in_id is None
    ):
        return None
    return build_session_cookie(
        settings,
        email=email,
        org_slug=org_slug,
        signed_in_at=iat,
        expires_at=min(exp, iat + SESSION_TTL),
        sign_in_id=sign_in_id,
    )


def _sign_in_id(payload: dict[str, Any]) -> str | None:
    """The Google sign-in a cookie belongs to. A cookie minted before
    ``sid`` existed is its own sign-in: its ``jti``."""
    for field in ("sid", "jti"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    return None


# The deny-list holds cookie ids (``jti``) and sign-in ids under this
# prefix, so the two kinds of id can never collide.
_SIGN_IN_KEY_PREFIX = "sign-in:"


def _deny_list_keys(
    payload: dict[str, Any], *, whole_sign_in: bool,
) -> list[str]:
    keys: list[str] = []
    jti = payload.get("jti")
    if isinstance(jti, str) and jti:
        keys.append(jti)
    sign_in_id = _sign_in_id(payload)
    if whole_sign_in and sign_in_id is not None:
        keys.append(_SIGN_IN_KEY_PREFIX + sign_in_id)
    return keys


def _is_time(value: object) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def set_session_cookie(
    response: Response, settings: Settings, cookie_value: str,
) -> None:
    """Hand ``cookie_value`` to the browser, kept until its own end."""
    payload = get_session_payload(settings, cookie_value)
    exp = payload.get("exp") if payload is not None else None
    seconds_left = math.ceil(exp - time.time()) if _is_time(exp) else 0
    response.set_cookie(
        COOKIE_NAME,
        cookie_value,
        httponly=True,
        samesite="lax",
        max_age=max(0, seconds_left),
        path="/",
    )


async def revoke_session(
    session_revocation: SessionRevocationStore | None,
    payload: dict[str, Any],
    *,
    whole_sign_in: bool,
) -> None:
    """Deny-list the cookie ``payload`` came from until its own end.

    ``whole_sign_in`` also deny-lists its sign-in id, which ends every
    cookie org switches made from the same Google sign-in.
    """
    if session_revocation is None:
        return
    exp = payload.get("exp")
    if not _is_time(exp):
        return
    for key in _deny_list_keys(payload, whole_sign_in=whole_sign_in):
        await session_revocation.revoke(key, exp - time.time())


async def is_session_revoked(
    session_revocation: SessionRevocationStore | None,
    payload: dict[str, Any],
) -> bool:
    """Whether the cookie, or the sign-in it belongs to, was ended."""
    if session_revocation is None:
        return False
    for key in _deny_list_keys(payload, whole_sign_in=True):
        if await session_revocation.is_revoked(key):
            return True
    return False


async def _maybe_auto_admin_default_org(
    email: str,
    runtime_manager: OrgRuntimeManager,
    policy_store: ConfigRepository,
    org_service: OrgService,
) -> bool:
    """Grant admin on the default org if it has no users yet.

    Standalone-only convenience (cloud users with zero memberships go
    to the signup page instead). Returns ``True`` if the caller was
    provisioned as admin, ``False`` if the org already had users and
    the caller should be treated as a stranger.
    """
    default_runtime = await runtime_manager.get(DEFAULT_ORG_ID)
    if default_runtime.policy_engine.config.users:
        return False
    admin_role = default_runtime.policy_engine.default_admin_role_name()
    user_def = UserDefinition(role=admin_role)
    new_config = await policy_store.add_first_user(
        DEFAULT_ORG_ID, email, user_def,
    )
    if new_config is None:
        # Another first sign-in (or this person's other tab) got there
        # first: the store checked "no users yet" in its write step.
        return False
    default_runtime.policy_engine.reload(new_config)
    # The person setting the install up is a member from the start, like
    # an org's creator: there is no invitation for them to accept.
    await org_service.add_founding_member(DEFAULT_ORG_ID, email, admin_role)
    runtime_manager.note_member_joined(DEFAULT_ORG_ID, email)
    logger.info(
        "dashboard.auth.first_login.auto_admin",
        email=email,
    )
    return True


@dataclass
class DashboardAuth:
    """Holds the auth router and its FastAPI dependencies for reuse."""
    router: APIRouter
    # Dependencies are async (they await the session-revocation store).
    # FastAPI accepts sync or async callables interchangeably for
    # ``Depends(...)``, so other routers don't need to change.
    get_current_user: Callable[..., Any]
    require_admin: Callable[..., Any]
    # Signed in, whatever org: see ``get_session_user``.
    get_session_user: Callable[..., Any]


def create_dashboard_auth(
    settings: Settings,
    runtime_manager: OrgRuntimeManager,
    policy_store: ConfigRepository,
    org_service: OrgService,
    dashboard_oauth: DashboardOAuthProvider,
    gateway_oauth_provider: McpGatewayOAuthProvider | None = None,
    session_revocation: SessionRevocationStore | None = None,
) -> DashboardAuth:
    router = APIRouter(prefix="/api/auth", tags=["dashboard-auth"])

    # Instance-level super-admin allowlist (cloud only; empty in
    # standalone). Super-admins act as admins in any org via the
    # cross-org dashboard, so they bypass the per-org membership /
    # admin-role checks below. Their identity is established from the
    # signed session cookie, and the org they operate on is set by
    # ``OrgContextMiddleware`` (X-Org-Slug header / ?org= param,
    # itself gated on the same allowlist).
    superadmin_emails = settings.parsed_superadmin_emails()

    async def get_session_user(
        request: Request,
        mcpolis_session: str | None = Cookie(default=None),
    ) -> str:
        """FastAPI dependency: the signed-in person, for routes that are
        not about one org's data (who am I, my orgs and invitations,
        creating or switching an org). A person with no org yet, or one
        who has only been invited, gets here with an empty org in their
        cookie.

        Mode-agnostic after the Phase 1 middleware unification:
        ``current_org_id`` is set by ``OrgContextMiddleware`` in both
        modes (default org in standalone via path injection, cookie-
        resolved org in cloud). If the user has been removed from
        their active org's policy, 403 — catches stale cookies after
        a Team-page removal without waiting for them to expire.
        """
        del request  # The cookie carries authentication, not headers.
        payload = get_session_payload(settings, mcpolis_session)
        if payload is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        # Revocation check: ``/logout`` (and future role-revocation
        # events) deny-list cookies by jti and sign-in id. Fail-open on backend
        # errors — the cookie is still signed + time-bounded.
        if await is_session_revoked(session_revocation, payload):
            raise HTTPException(
                status_code=401, detail="Session revoked",
            )
        email = payload.get("email")
        if not isinstance(email, str):
            raise HTTPException(status_code=401, detail="Not authenticated")
        # Only enforce the "user still in policy" check when the
        # request is scoped to a specific org. A fresh login with no
        # memberships (empty cookie org_slug) legitimately has no
        # role anywhere yet — the frontend routes that user to the
        # signup page.
        slug = current_org_slug.get()
        if slug and email not in superadmin_emails:
            # Super-admins legitimately have no role row in a foreign
            # org they're browsing via the cross-org dashboard, so the
            # "still in policy" check would wrongly 403 them.
            # ``get`` (not ``get_cached``): an org this process has not
            # loaded yet (the boot window, or one another backend
            # created) must still be checked, not waved through.
            org_id = current_org_id.get()
            runtime = await runtime_manager.get(org_id)
            if not runtime.policy_engine.get_user_roles(email):
                raise HTTPException(
                    status_code=403, detail="User has been removed"
                )
        return email

    async def get_current_user(
        request: Request,
        mcpolis_session: str | None = Cookie(default=None),
    ) -> str:
        """FastAPI dependency: a signed-in MEMBER of the request's org
        (or an MCP Hero operator browsing it), for every route about that
        org's data.

        Unlike ``get_session_user`` it also refuses an empty org in the
        cookie: the request then falls back to the default org, which
        someone with no org, or only an invitation to it, must not see.
        """
        email = await get_session_user(request, mcpolis_session)
        if email in superadmin_emails:
            return email
        runtime = await runtime_manager.get(current_org_id.get())
        if not runtime.policy_engine.get_user_roles(email):
            raise HTTPException(
                status_code=403, detail="Not a member of this organization",
            )
        return email

    async def require_admin(
        email: str = Depends(get_current_user),
    ) -> str:
        """FastAPI dependency: require admin role.

        In cloud mode the global ``policy_engine`` may not reflect the
        current org's config (it's loaded from the default org at
        startup, which may not exist). Fall back to checking the
        current org's config via ``policy_store`` directly.
        """
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        if runtime.policy_engine.is_admin(email):
            acting_as_operator.set(False)
            return email
        # Super-admins act as admins in any org (the cross-org
        # dashboard drill-down). The org context here is already the
        # one the super-admin targeted via OrgContextMiddleware. Audit
        # rows written during this request are tagged "operator".
        if email in superadmin_emails:
            acting_as_operator.set(True)
            return email
        raise HTTPException(status_code=403, detail="Admin role required")

    @router.get("/login")
    async def login(join: str | None = None) -> Response:
        state = secrets.token_urlsafe(32)
        _pending_logins[state] = (time.time(), join)
        callback_url = f"{settings.server_url.rstrip('/')}/api/auth/callback"
        url = await dashboard_oauth.start_login(
            state=state, redirect_uri=callback_url, join=join,
        )
        return RedirectResponse(url=url)

    @router.get("/callback")
    async def callback(
        code: str = "",
        state: str = "",
        error: str = "",
    ) -> Response:
        if error:
            raise HTTPException(400, f"Auth error: {error}")
        if not code or not state:
            raise HTTPException(400, "Missing code or state")

        # Validate state
        pending = _pending_logins.pop(state, None)
        if pending is None or time.time() - pending[0] > 600:
            raise HTTPException(400, "Invalid or expired state")
        join_slug = pending[1]

        callback_url = f"{settings.server_url.rstrip('/')}/api/auth/callback"
        try:
            completed = await dashboard_oauth.complete_login(
                code=code, state=state, redirect_uri=callback_url,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        email = completed.email

        # One login flow for both modes. Signing in never accepts an
        # invitation: an invited person becomes a member only by
        # clicking Join on that org's invitation.
        #
        # 1. List the orgs the person is a member of.
        # 2. Fresh standalone install (default org has no users yet)
        #    → auto-admin the first login. Standalone stranger (not a
        #    member, not invited) → send them home with an
        #    "auth_error=not_a_member" banner so the admin knows to
        #    invite them. Cloud users with zero memberships just get
        #    an empty cookie — the frontend renders the SignupPage
        #    from ``current_org=null``, which lists their invitations.
        # 3. Choose an org (join-link override, else first membership)
        #    and set the session cookie. A person still to accept the
        #    invitation they came for lands on that org's Join page.
        orgs = await org_service.list_user_orgs(email)

        was_first_user_auto_admin = False
        # The org whose Join page to land on: an invitation still to
        # accept.
        invited_slug: str | None = None
        if not orgs and settings.mode == "standalone":
            provisioned = await _maybe_auto_admin_default_org(
                email, runtime_manager, policy_store, org_service,
            )
            if provisioned:
                orgs = await org_service.list_user_orgs(email)
                was_first_user_auto_admin = True
            else:
                # Standalone has one org: an invited person accepts its
                # invitation on its Join page. Anyone else isn't in
                # config.users; self-hosted installs use this redirect
                # to nudge the admin to add the user.
                invitations = await org_service.list_invitations(email)
                if not invitations:
                    params = urlencode(
                        {"auth_error": "not_a_member", "email": email},
                    )
                    return RedirectResponse(
                        url=f"/?{params}", status_code=302,
                    )
                invited_slug = invitations[0].org.slug

        if join_slug and any(o.slug == join_slug for o in orgs):
            chosen_slug = join_slug
        elif join_slug:
            # Came from a join link without being a member of that org:
            # either invited (accept on its Join page) or a stranger.
            if await org_service.invitation_to(join_slug, email) is None:
                params = urlencode(
                    {"auth_error": "not_a_member", "email": email, "org": join_slug},
                )
                return RedirectResponse(
                    url=f"/orgs/{join_slug}/join?{params}", status_code=302,
                )
            invited_slug = join_slug
            chosen_slug = orgs[0].slug if orgs else ""
        else:
            chosen_slug = orgs[0].slug if orgs else ""

        cookie_value = build_session_cookie(
            settings, email=email, org_slug=chosen_slug,
        )
        response = RedirectResponse(
            url=f"/orgs/{invited_slug}/join" if invited_slug else "/",
            status_code=302,
        )
        set_session_cookie(response, settings, cookie_value)
        get_analytics().track_async(
            email,
            "user_logged_in",
            {
                "auth_method": dashboard_oauth.name,
                "was_first_user_auto_admin": was_first_user_auto_admin,
            },
        )
        return response

    @router.get("/status", response_model=AuthStatus)
    async def status() -> AuthStatus:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        return AuthStatus(has_users=bool(runtime.policy_engine.config.users))

    @router.get("/me", response_model=AuthUserInfo)
    async def me(
        email: str = Depends(get_session_user),
        mcpolis_session: str | None = Cookie(default=None),
    ) -> AuthUserInfo:
        """Resolve the caller's memberships + active org + admin flag.

        One code path for both modes — ``OrgService.list_user_orgs``
        falls back to the default-org config when no membership rows
        exist (the standalone case). Per-org ``is_admin`` comes from
        ``OrgService.is_admin``, which routes through
        ``RoleDefinition.is_admin`` rather than the role's name.
        """
        orgs_out: list[OrgMembership] = []
        current_out: CurrentOrgInfo | None = None
        is_admin = False

        user_orgs = await org_service.list_user_orgs(email)
        org_by_slug = {org.slug: org for org in user_orgs}
        # Track per-org is_admin so we can populate both the membership
        # row and the active-org payload without re-loading the config.
        is_admin_by_slug: dict[str, bool] = {}
        for org in user_orgs:
            role = await org_service.get_user_role(org.id, email) or ""
            org_is_admin = await org_service.is_admin(org.id, email)
            is_admin_by_slug[org.slug] = org_is_admin
            orgs_out.append(
                OrgMembership(
                    slug=org.slug,
                    display_name=org.display_name,
                    role=role,
                    is_admin=org_is_admin,
                    plan=org.subscription.plan.value,
                ),
            )

        # Active org comes from the cookie's org_slug claim. In
        # standalone the cookie holds "default"; in cloud it's the
        # user's chosen org. When there's no cookie (dev mode uses
        # a header for auth and never sets one) or the cookie's slug
        # no longer matches a membership (org deleted, user removed),
        # fall back to the first membership so the UI has something
        # to render.
        payload = get_session_payload(settings, mcpolis_session)
        cookie_slug = (
            payload.get("org_slug") if payload is not None else None
        )
        match: OrgMembership | None = None
        if isinstance(cookie_slug, str) and cookie_slug:
            match = next(
                (o for o in orgs_out if o.slug == cookie_slug), None,
            )
        if match is None and orgs_out:
            match = orgs_out[0]
        if match is not None:
            org_for_match = org_by_slug.get(match.slug)
            plan_value = (
                org_for_match.subscription.plan.value
                if org_for_match is not None else "free"
            )
            current_out = CurrentOrgInfo(
                slug=match.slug,
                display_name=match.display_name,
                role=match.role,
                is_admin=is_admin_by_slug.get(match.slug, False),
                plan=plan_value,
            )

        # Admin flag reflects the active org's policy config, so a user
        # with admin in org A and viewer in org B sees the right flag
        # depending on the cookie.
        if current_out is not None:
            is_admin = current_out.is_admin
        roles = [current_out.role] if current_out else []

        # Instance-level superadmin flag (cloud only). Same allowlist
        # used to gate /admin-mcp/system. Frontend uses this to surface
        # superadmin-only UI like /__debug.
        is_superadmin = email in superadmin_emails

        # Invitations still to accept or decline: the dashboard shows
        # them where the person lands after sign-in.
        invitations = [
            InvitationInfo(
                slug=invitation.org.slug,
                display_name=invitation.org.display_name,
                role=invitation.role,
            )
            for invitation in await org_service.list_invitations(email)
        ]

        return AuthUserInfo(
            email=email,
            roles=roles,
            is_admin=is_admin,
            is_superadmin=is_superadmin,
            orgs=orgs_out,
            current_org=current_out,
            invitations=invitations,
        )

    # Test-only endpoint: mint a gateway bearer token for any email
    # without Google OAuth. Only registered when ``test_mode`` is set
    # at startup — never reachable in cloud mode unless bound to a
    # loopback address (the startup validator enforces that). Dashboard
    # cookies aren't minted here anymore — tests walk the dev-stub
    # provider's /login flow and get a real signed cookie that way.
    if settings.test_mode:
        @router.post("/test-mcp-token")
        async def test_mcp_token(request: Request) -> dict[str, Any]:
            # Web pages are refused. CORS allows every origin app-wide,
            # and this route mints a bearer for any email with no
            # credential, so any website open in the operator's browser
            # while ``start.sh --fake-auth`` runs could otherwise mint an
            # admin bearer and drive the admin MCP, whose host check is
            # off (``mcp_transport_security``). A browser always sends
            # Origin on such a request; the real callers (the e2e
            # helpers and orchestrator) send none.
            if "origin" in request.headers:
                raise HTTPException(403, "Not available to web pages")
            if gateway_oauth_provider is None:
                raise HTTPException(400, "OAuth is not enabled")
            body = await request.json()
            email = body.get("email")
            org_slug_param = body.get("org_slug")
            if not isinstance(email, str) or not isinstance(org_slug_param, str):
                raise HTTPException(400, "email and org_slug required")

            # ``org_slug_param`` is no longer used by the user-scoped
            # token model — kept in the request body for backwards
            # compatibility with the test-mode CLI helper that still
            # passes it. We resolve it only to make sure the slug is
            # valid (so a typo on the test side fails loudly). Minting a
            # token accepts no invitation, as a real sign-in doesn't.
            await org_service.resolve_slug(org_slug_param)
            token: str = await gateway_oauth_provider.mint_test_token(email)
            return {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
            }

    @router.post("/logout")
    async def logout(
        mcpolis_session: str | None = Cookie(default=None),
    ) -> Response:
        # Deny-list the cookie's jti and sign-in id so no
        # still-signed-and-unexpired cookie of this sign-in (including
        # those org switches made from it) can be replayed (stolen
        # laptop, shared machine, etc.).
        # Only the owner of the cookie can revoke it — the HMAC sig is
        # verified before we ever look at the jti.
        payload = get_session_payload(settings, mcpolis_session)
        if payload is not None:
            await revoke_session(
                session_revocation, payload, whole_sign_in=True,
            )
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # Provider-owned routes (dev-stub picker page + submit handler).
    # Google has no extra routes; only the dev-stub uses this hook.
    register_routes = getattr(dashboard_oauth, "register_routes", None)
    if callable(register_routes):
        register_routes(router, suggested_emails=_suggested_emails(runtime_manager))

    return DashboardAuth(
        router=router,
        get_current_user=get_current_user,
        require_admin=require_admin,
        get_session_user=get_session_user,
    )


def _suggested_emails(runtime_manager: OrgRuntimeManager) -> list[str]:
    """Build the dev-stub picker shortlist from the default org's policy.

    Admins come first (their position in the dropdown becomes the
    pre-filled default — one-click login as a working admin), followed
    by everyone else. Sources the live in-memory runtime so a freshly
    added user shows up without a restart. Returns ``[]`` if the
    default org hasn't been initialised yet.
    """
    runtime = runtime_manager.get_cached(DEFAULT_ORG_ID)
    if runtime is None:
        return []
    config = runtime.policy_engine.config
    admins: list[str] = []
    others: list[str] = []
    for email, user_def in config.users.items():
        role_def = config.roles.get(user_def.role)
        is_admin = bool(role_def and role_def.is_admin)
        (admins if is_admin else others).append(email)
    return sorted(admins) + sorted(others)
