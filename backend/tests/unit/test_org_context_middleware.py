"""Tests for OrgContextMiddleware slug→org_id resolution + path rewrite.

The first section tests the static ``_split_slug_prefix`` helper.
The second section drives the full ASGI ``__call__`` through a mock
downstream app so we can snapshot:
- the downstream path the mounted app sees (after any rewrite)
- the contextvars ``current_org_id`` / ``current_org_slug`` set for the
  duration of the downstream call
- any response the middleware produced itself (e.g. 401 rejections)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import structlog
from starlette.types import Message, Receive, Scope, Send

from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.organization_repository import Organization
from mcpolis.domain.services.org_service import OrgNotFoundError
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_org_slug,
)
from mcpolis.entrypoints.middleware.org_context import (
    OrgContextMiddleware,
    SlugCache,
)
from mcpolis.entrypoints.routes.dashboard_auth import build_session_cookie


# ── _split_slug_prefix (existing tests) ──────────────────────────────


def test_split_slug_prefix_mcp() -> None:
    result = OrgContextMiddleware._split_slug_prefix("/mcp/acme/foo", "/mcp/")
    assert result is not None
    slug, remaining = result
    assert slug == "acme"
    assert remaining == "/mcp/foo"


def test_split_slug_prefix_admin_mcp() -> None:
    result = OrgContextMiddleware._split_slug_prefix("/admin-mcp/beta/bar", "/admin-mcp/")
    assert result is not None
    slug, remaining = result
    assert slug == "beta"
    assert remaining == "/admin-mcp/bar"


def test_split_slug_prefix_slug_only_no_trailing() -> None:
    result = OrgContextMiddleware._split_slug_prefix("/mcp/acme", "/mcp/")
    assert result is not None
    slug, remaining = result
    assert slug == "acme"
    assert remaining == "/mcp/"


def test_split_slug_prefix_no_match() -> None:
    result = OrgContextMiddleware._split_slug_prefix("/api/orgs", "/mcp/")
    assert result is None


def test_split_slug_prefix_empty_after_prefix() -> None:
    result = OrgContextMiddleware._split_slug_prefix("/mcp/", "/mcp/")
    assert result is None


def test_split_slug_prefix_wellknown_is_not_slug() -> None:
    """The middleware skips .well-known, oauth, etc. as known non-slug segments."""
    result = OrgContextMiddleware._split_slug_prefix("/mcp/.well-known/foo", "/mcp/")
    assert result is not None
    slug, _ = result
    assert slug == ".well-known"
    # The middleware itself checks if slug is in a skip-list and bails
    # before resolving. This test just verifies the split works — the
    # skip logic is in the __call__ method.


# ── ASGI __call__ coverage ───────────────────────────────────────────


@dataclass
class MiddlewareResult:
    """Snapshot of what the middleware did for a single request."""
    downstream_path: str | None
    """Path the mount saw (after any rewrite). None if the middleware
    rejected the request before calling downstream."""
    org_id: str | None
    """Value of ``current_org_id.get()`` inside the downstream call."""
    org_slug: str | None
    """Value of ``current_org_slug.get()`` inside the downstream call."""
    status: int
    """HTTP status code sent. 200 when downstream ran normally."""


class FakeOrgService:
    """Minimal stand-in for ``OrgService`` — only ``resolve_slug`` is
    called by the middleware."""

    def __init__(self, slugs: dict[str, str]) -> None:
        # slug -> org_id mapping. An absent key raises OrgNotFoundError.
        self._slugs = slugs

    async def resolve_slug(self, slug: str) -> Organization:
        from datetime import UTC, datetime
        if slug not in self._slugs:
            raise OrgNotFoundError(slug)
        return Organization(
            id=self._slugs[slug],
            slug=slug,
            display_name=slug,
            created_at=datetime.now(UTC),
        )


def make_settings(
    mode: str = "cloud",
    session_secret: str = "middleware-test-secret-0123456789",
    superadmin_emails: str = "",
) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode=mode,  # type: ignore[arg-type]
        session_secret=session_secret,
        superadmin_emails=superadmin_emails,
    )


def make_org_service(slugs: dict[str, str] | None = None) -> FakeOrgService:
    return FakeOrgService(
        slugs if slugs is not None else {"default": DEFAULT_ORG_ID},
    )


def build_middleware(
    settings: Settings,
    org_service: FakeOrgService | None = None,
    monotonic: Any = None,
) -> tuple[OrgContextMiddleware, dict[str, Any]]:
    """Build a middleware over a mock downstream ASGI app.

    Returns ``(middleware, captured)``. ``captured`` is refreshed on
    every downstream call with the path / org_id / org_slug the mount
    saw. Reuse the same returned middleware across multiple ``drive``
    calls to exercise instance state (the slug cache, the
    super-admin-access log throttle).
    """
    slug_cache = SlugCache()
    org_svc = org_service or make_org_service()
    captured: dict[str, Any] = {}

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        captured["path"] = scope.get("path", "")
        captured["org_id"] = current_org_id.get()
        captured["org_slug"] = current_org_slug.get()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [],
        })
        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        })

    kwargs: dict[str, Any] = {}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    middleware = OrgContextMiddleware(
        downstream, settings, org_svc,  # type: ignore[arg-type]
        slug_cache, **kwargs,
    )
    return middleware, captured


async def drive(
    middleware: OrgContextMiddleware,
    captured: dict[str, Any],
    path: str,
    *,
    cookie_header: str | None = None,
    org_slug_header: str | None = None,
    query_string: str = "",
) -> MiddlewareResult:
    """Send one request through an already-built middleware."""
    captured.clear()

    headers: list[tuple[bytes, bytes]] = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode("latin-1")))
    if org_slug_header is not None:
        headers.append((b"x-org-slug", org_slug_header.encode("latin-1")))

    scope: Scope = {
        "type": "http",
        "path": path,
        # utf-8, not latin-1: a real server percent-encodes, and a
        # test path may hold non-Latin characters.
        "raw_path": path.encode("utf-8"),
        "headers": headers,
        "query_string": query_string.encode("latin-1"),
        "method": "GET",
    }

    statuses: list[int] = []

    async def send(event: Message) -> None:
        if event["type"] == "http.response.start":
            statuses.append(int(event.get("status", 0)))

    async def receive() -> Message:
        return {
            "type": "http.request", "body": b"", "more_body": False,
        }

    await middleware(scope, receive, send)

    status = statuses[0] if statuses else 0
    if "path" not in captured:
        # Middleware replied itself — downstream was never called.
        return MiddlewareResult(
            downstream_path=None, org_id=None, org_slug=None, status=status,
        )
    return MiddlewareResult(
        downstream_path=captured["path"],
        org_id=captured["org_id"],
        org_slug=captured["org_slug"],
        status=status,
    )


async def run_middleware(
    settings: Settings,
    path: str,
    *,
    cookie_header: str | None = None,
    org_slug_header: str | None = None,
    query_string: str = "",
    org_service: FakeOrgService | None = None,
) -> MiddlewareResult:
    """Drive a fresh middleware with a mock downstream for one request."""
    middleware, captured = build_middleware(settings, org_service)
    return await drive(
        middleware, captured, path,
        cookie_header=cookie_header,
        org_slug_header=org_slug_header,
        query_string=query_string,
    )


# ── Standalone mode ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_standalone_mcp_path_pins_default_org() -> None:
    """Standalone injects ``/default`` so the path goes through the
    same slug-aware pipeline as cloud. External URL stays slug-less
    (``/mcp/my-tools``) — downstream still sees a slug-stripped path
    and ``current_org_slug`` is ``default``."""
    settings = make_settings(mode="standalone")

    result = await run_middleware(settings, "/mcp/my-tools")

    assert result.status == 200
    assert result.downstream_path == "/mcp/my-tools"
    assert result.org_id == DEFAULT_ORG_ID
    assert result.org_slug == "default"


@pytest.mark.asyncio
async def test_standalone_admin_mcp_path_pins_default_org() -> None:
    settings = make_settings(mode="standalone")

    result = await run_middleware(settings, "/admin-mcp/foo")

    assert result.status == 200
    assert result.downstream_path == "/admin-mcp/foo"
    assert result.org_id == DEFAULT_ORG_ID
    assert result.org_slug == "default"


@pytest.mark.asyncio
async def test_standalone_bare_mcp_routes_to_default() -> None:
    """Bare ``/mcp`` (no trailing slash or segment) in standalone
    still routes through the default org. The injection makes it
    ``/mcp/default/``, the slug resolves to DEFAULT_ORG_ID, and the
    rewrite strips the slug back to ``/mcp/`` for the downstream
    mount."""
    settings = make_settings(mode="standalone")

    result = await run_middleware(settings, "/mcp")

    assert result.status == 200
    assert result.downstream_path == "/mcp/"
    assert result.org_id == DEFAULT_ORG_ID
    assert result.org_slug == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("segment", [
    ".well-known", "oauth", "register", "token", "authorize",
    "callback", "system",
])
async def test_standalone_reserved_segments_are_not_injected(segment: str) -> None:
    """Reserved segments (OAuth discovery, DCR, token, admin-mcp
    system) keep their original path in standalone — the injection
    logic must not prepend ``/default`` before them."""
    settings = make_settings(mode="standalone")

    result = await run_middleware(settings, f"/mcp/{segment}/foo")

    assert result.status == 200
    assert result.downstream_path == f"/mcp/{segment}/foo"
    assert result.org_id == DEFAULT_ORG_ID
    # No slug was resolved for the reserved segment.
    assert result.org_slug == ""


@pytest.mark.asyncio
async def test_standalone_api_path_pins_default_org() -> None:
    settings = make_settings(mode="standalone")

    result = await run_middleware(settings, "/api/auth/status")

    assert result.status == 200
    assert result.org_id == DEFAULT_ORG_ID
    assert result.org_slug == ""


@pytest.mark.asyncio
async def test_standalone_wellknown_path_pins_default_org() -> None:
    settings = make_settings(mode="standalone")

    result = await run_middleware(settings, "/.well-known/oauth-protected-resource")

    assert result.status == 200
    assert result.downstream_path == "/.well-known/oauth-protected-resource"
    assert result.org_id == DEFAULT_ORG_ID


# ── Cloud mode: slug resolution ──────────────────────────────────────


@pytest.mark.asyncio
async def test_cloud_mcp_slug_scopes_to_org_and_rewrites_path() -> None:
    """``/mcp/{slug}/...`` resolves the slug, pins ``current_org_id``
    to that org, and rewrites the path so the downstream mount sees
    ``/mcp/...`` (no slug). This is the URL surfaced in the product
    UI; the gateway controller's single-org list_tools path runs."""
    settings = make_settings(mode="cloud")
    org_svc = make_org_service({"acme": "acme-org-id"})

    result = await run_middleware(settings, "/mcp/acme/foo", org_service=org_svc)

    assert result.status == 200
    assert result.downstream_path == "/mcp/foo"
    assert result.org_id == "acme-org-id"
    assert result.org_slug == "acme"


@pytest.mark.asyncio
async def test_cloud_mcp_slug_no_trailing_resolves_and_rewrites() -> None:
    """``/mcp/{slug}`` (no trailing slash, the bare slug-scoped
    handshake URL) resolves the slug and rewrites to ``/mcp/`` so the
    streamable-HTTP transport's exact mount path is hit."""
    settings = make_settings(mode="cloud")
    org_svc = make_org_service({"acme": "acme-org-id"})

    result = await run_middleware(settings, "/mcp/acme", org_service=org_svc)

    assert result.status == 200
    assert result.downstream_path == "/mcp/"
    assert result.org_id == "acme-org-id"
    assert result.org_slug == "acme"


@pytest.mark.asyncio
async def test_cloud_mcp_unknown_slug_returns_401() -> None:
    """Unknown slug under ``/mcp/{slug}/...`` 401s with the same
    anti-enumeration behaviour as ``/admin-mcp/{slug}/...``."""
    settings = make_settings(mode="cloud")
    org_svc = make_org_service({"acme": "acme-org-id"})  # beta not present

    result = await run_middleware(settings, "/mcp/beta/foo", org_service=org_svc)

    assert result.status == 401
    assert result.downstream_path is None  # downstream never called


@pytest.mark.asyncio
async def test_cloud_resolves_slug_and_rewrites_admin_mcp_path() -> None:
    settings = make_settings(mode="cloud")
    org_svc = make_org_service({"beta": "beta-org-id"})

    result = await run_middleware(settings, "/admin-mcp/beta/bar", org_service=org_svc)

    assert result.status == 200
    assert result.downstream_path == "/admin-mcp/bar"
    assert result.org_id == "beta-org-id"
    assert result.org_slug == "beta"


@pytest.mark.asyncio
async def test_cloud_admin_mcp_unknown_slug_returns_401() -> None:
    """Admin MCP keeps slug-based routing — unknown slug 401s as before."""
    settings = make_settings(mode="cloud")
    org_svc = make_org_service({"acme": "acme-org-id"})  # beta not present

    result = await run_middleware(settings, "/admin-mcp/beta/bar", org_service=org_svc)

    assert result.status == 401
    assert result.downstream_path is None  # downstream never called


# ── Cloud mode: reserved path segments under /mcp/ ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("segment", [
    ".well-known",
    "oauth",
    "register",
    "token",
    "authorize",
    "callback",
    "system",
])
async def test_cloud_reserved_mcp_segment_is_not_rewritten(segment: str) -> None:
    """Reserved segments like /mcp/.well-known/... or /mcp/oauth/...
    must not be treated as slugs. They fall through to the silent
    DEFAULT_ORG_ID fallback today — Phase 2 keeps this behaviour for
    reserved segments (only bare /mcp/ flips to 401)."""
    settings = make_settings(mode="cloud")

    result = await run_middleware(settings, f"/mcp/{segment}/foo")

    assert result.status == 200
    assert result.downstream_path == f"/mcp/{segment}/foo"
    assert result.org_id == DEFAULT_ORG_ID
    # No slug was resolved — empty.
    assert result.org_slug == ""


# ── Cloud mode: cookie-based org resolution for /api/* ───────────────


@pytest.mark.asyncio
async def test_cloud_api_path_resolves_org_from_session_cookie() -> None:
    settings = make_settings(mode="cloud")
    org_svc = make_org_service({"acme": "acme-org-id"})
    cookie_value = build_session_cookie(
        settings, email="alice@acme.com", org_slug="acme",
    )
    cookie_header = f"mcpolis_session={cookie_value}"

    result = await run_middleware(
        settings, "/api/admin/upstreams",
        cookie_header=cookie_header, org_service=org_svc,
    )

    assert result.status == 200
    # /api/* is not rewritten
    assert result.downstream_path == "/api/admin/upstreams"
    assert result.org_id == "acme-org-id"
    assert result.org_slug == "acme"


@pytest.mark.asyncio
async def test_cloud_api_path_with_no_cookie_falls_back_to_default() -> None:
    """Public /api/* endpoints (login, signup) run before the user has a
    session cookie. They hit the silent DEFAULT_ORG_ID fallback — Phase
    2 preserves this (only bare /mcp/* flips to 401)."""
    settings = make_settings(mode="cloud")

    result = await run_middleware(settings, "/api/auth/status")

    assert result.status == 200
    assert result.org_id == DEFAULT_ORG_ID
    assert result.org_slug == ""


@pytest.mark.asyncio
async def test_cloud_api_path_cookie_for_unknown_slug_falls_back_to_default() -> None:
    """If the cookie's org_slug can't be resolved (org deleted, cookie
    stale), the middleware falls back to DEFAULT_ORG_ID silently
    rather than 401-ing — the downstream endpoint can decide what to
    do with an authenticated-but-orgless state."""
    settings = make_settings(mode="cloud")
    org_svc = make_org_service({"acme": "acme-org-id"})  # "stale" not present
    cookie_value = build_session_cookie(
        settings, email="alice@acme.com", org_slug="stale",
    )
    cookie_header = f"mcpolis_session={cookie_value}"

    result = await run_middleware(
        settings, "/api/admin/upstreams",
        cookie_header=cookie_header, org_service=org_svc,
    )

    assert result.status == 200
    assert result.org_id == DEFAULT_ORG_ID
    assert result.org_slug == ""


# ── Cloud mode: super-admin cross-org override (X-Org-Slug / ?org=) ──


def _superadmin_cookie(settings: Settings, *, org_slug: str) -> str:
    cookie_value = build_session_cookie(
        settings, email="super@admin.com", org_slug=org_slug,
    )
    return f"mcpolis_session={cookie_value}"


def _member_cookie(
    settings: Settings, *, email: str, org_slug: str,
) -> str:
    cookie_value = build_session_cookie(
        settings, email=email, org_slug=org_slug,
    )
    return f"mcpolis_session={cookie_value}"


@pytest.mark.asyncio
async def test_superadmin_x_org_slug_header_overrides_cookie_org() -> None:
    """A super-admin (verified from the signed cookie) drilling into
    another org sends ``X-Org-Slug``; the middleware resolves that org
    instead of the cookie's own-org slug. This is what lets the
    cross-org dashboard's upstream detail page load without rotating
    the session cookie."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service(
        {"home": "home-org-id", "acme": "acme-org-id"},
    )
    result = await run_middleware(
        settings, "/api/admin/upstreams/up-1",
        cookie_header=_superadmin_cookie(settings, org_slug="home"),
        org_slug_header="acme",
        org_service=org_svc,
    )

    assert result.status == 200
    assert result.org_id == "acme-org-id"
    assert result.org_slug == "acme"


@pytest.mark.asyncio
async def test_superadmin_org_query_param_overrides_cookie_org() -> None:
    """EventSource can't set request headers, so the SSE streams pass
    the target org as ``?org=``. The middleware honours it identically
    for super-admins."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service(
        {"home": "home-org-id", "acme": "acme-org-id"},
    )
    result = await run_middleware(
        settings, "/api/events",
        cookie_header=_superadmin_cookie(settings, org_slug="home"),
        query_string="org=acme",
        org_service=org_svc,
    )

    assert result.status == 200
    assert result.org_id == "acme-org-id"
    assert result.org_slug == "acme"


@pytest.mark.asyncio
async def test_non_superadmin_x_org_slug_header_is_ignored() -> None:
    """A normal user setting ``X-Org-Slug`` to a foreign org gets NO
    override — the cookie's own-org context stands. This is the
    privilege-escalation guard: the override is gated strictly on
    super-admin identity."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service(
        {"home": "home-org-id", "acme": "acme-org-id"},
    )
    result = await run_middleware(
        settings, "/api/admin/upstreams/up-1",
        cookie_header=_member_cookie(
            settings, email="bob@home.com", org_slug="home",
        ),
        org_slug_header="acme",
        org_service=org_svc,
    )

    assert result.status == 200
    assert result.org_id == "home-org-id"
    assert result.org_slug == "home"


@pytest.mark.asyncio
async def test_superadmin_without_override_uses_cookie_org() -> None:
    """Absent any override header/param, even a super-admin lands in
    their own cookie org — they're a normal admin of their home org by
    default."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service(
        {"home": "home-org-id", "acme": "acme-org-id"},
    )
    result = await run_middleware(
        settings, "/api/admin/upstreams",
        cookie_header=_superadmin_cookie(settings, org_slug="home"),
        org_service=org_svc,
    )

    assert result.status == 200
    assert result.org_id == "home-org-id"
    assert result.org_slug == "home"


@pytest.mark.asyncio
async def test_superadmin_override_header_beats_query_param() -> None:
    """When both are present the header wins (matches the resolution
    order: header first, query param as the EventSource-only
    fallback)."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service(
        {"home": "home-org-id", "acme": "acme-org-id", "beta": "beta-org-id"},
    )
    result = await run_middleware(
        settings, "/api/admin/upstreams/up-1",
        cookie_header=_superadmin_cookie(settings, org_slug="home"),
        org_slug_header="acme",
        query_string="org=beta",
        org_service=org_svc,
    )

    assert result.status == 200
    assert result.org_id == "acme-org-id"
    assert result.org_slug == "acme"


@pytest.mark.asyncio
async def test_superadmin_override_unknown_slug_falls_back_to_default() -> None:
    """An unresolvable override slug (typo, deleted org) falls back to
    the silent DEFAULT_ORG_ID context just like a stale cookie slug —
    the super-admin is already trusted, so anti-enumeration 401s aren't
    needed here."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service({"home": "home-org-id"})  # "ghost" absent
    result = await run_middleware(
        settings, "/api/admin/upstreams/up-1",
        cookie_header=_superadmin_cookie(settings, org_slug="home"),
        org_slug_header="ghost",
        org_service=org_svc,
    )

    assert result.status == 200
    assert result.org_id == DEFAULT_ORG_ID
    assert result.org_slug == ""


# ── Cloud mode: super-admin cross-org access audit log ───────────────


@pytest.mark.asyncio
async def test_superadmin_drilldown_emits_access_audit_log() -> None:
    """A super-admin drilling into a foreign org emits one
    ``superadmin.org_access`` line carrying actor + target org, so the
    otherwise-silent override leaves an audit trail."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service(
        {"home": "home-org-id", "acme": "acme-org-id"},
    )
    middleware, captured = build_middleware(settings, org_svc)
    with structlog.testing.capture_logs() as logs:
        result = await drive(
            middleware, captured, "/api/admin/upstreams/up-1",
            cookie_header=_superadmin_cookie(settings, org_slug="home"),
            org_slug_header="acme",
        )

    assert result.org_id == "acme-org-id"
    access = [e for e in logs if e["event"] == "superadmin.org_access"]
    assert len(access) == 1
    assert access[0]["actor"] == "super@admin.com"
    assert access[0]["org_slug"] == "acme"
    assert access[0]["org_id"] == "acme-org-id"


@pytest.mark.asyncio
async def test_superadmin_own_org_override_emits_no_access_log() -> None:
    """An override that resolves to the super-admin's own cookie org is
    not a cross-org drill-down — no audit line."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service({"home": "home-org-id"})
    middleware, captured = build_middleware(settings, org_svc)
    with structlog.testing.capture_logs() as logs:
        await drive(
            middleware, captured, "/api/admin/upstreams/up-1",
            cookie_header=_superadmin_cookie(settings, org_slug="home"),
            org_slug_header="home",
        )

    assert not [e for e in logs if e["event"] == "superadmin.org_access"]


@pytest.mark.asyncio
async def test_non_superadmin_override_emits_no_access_log() -> None:
    """A normal user's ignored override grants no access and logs
    nothing — the gate is super-admin-only on both axes."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service(
        {"home": "home-org-id", "acme": "acme-org-id"},
    )
    middleware, captured = build_middleware(settings, org_svc)
    with structlog.testing.capture_logs() as logs:
        result = await drive(
            middleware, captured, "/api/admin/upstreams/up-1",
            cookie_header=_member_cookie(
                settings, email="bob@home.com", org_slug="home",
            ),
            org_slug_header="acme",
        )

    assert result.org_id == "home-org-id"
    assert not [e for e in logs if e["event"] == "superadmin.org_access"]


@pytest.mark.asyncio
async def test_superadmin_unknown_override_slug_emits_no_access_log() -> None:
    """An override slug that doesn't resolve (typo / deleted org) never
    reached a foreign org, so it logs no access line."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service({"home": "home-org-id"})  # "ghost" absent
    middleware, captured = build_middleware(settings, org_svc)
    with structlog.testing.capture_logs() as logs:
        await drive(
            middleware, captured, "/api/admin/upstreams/up-1",
            cookie_header=_superadmin_cookie(settings, org_slug="home"),
            org_slug_header="ghost",
        )

    assert not [e for e in logs if e["event"] == "superadmin.org_access"]


@pytest.mark.asyncio
async def test_superadmin_access_log_throttled_per_org() -> None:
    """Repeat drill-downs into the same org inside the throttle window
    emit one line; a different org is keyed separately and emits its
    own; past the window the same org logs again. A controllable
    monotonic clock drives the window."""
    settings = make_settings(
        mode="cloud", superadmin_emails="super@admin.com",
    )
    org_svc = make_org_service(
        {"home": "home-org-id", "acme": "acme-org-id", "beta": "beta-org-id"},
    )
    clock = {"t": 1000.0}
    middleware, captured = build_middleware(
        settings, org_svc, monotonic=lambda: clock["t"],
    )
    cookie = _superadmin_cookie(settings, org_slug="home")

    with structlog.testing.capture_logs() as logs:
        # First acme hit logs.
        await drive(
            middleware, captured, "/api/admin/upstreams/up-1",
            cookie_header=cookie, org_slug_header="acme",
        )
        # +30s: still inside the 60s window for acme — throttled.
        clock["t"] += 30.0
        await drive(
            middleware, captured, "/api/admin/upstreams/up-1",
            cookie_header=cookie, org_slug_header="acme",
        )
        # beta is a separate (actor, org) key — logs immediately.
        await drive(
            middleware, captured, "/api/admin/upstreams/up-1",
            cookie_header=cookie, org_slug_header="beta",
        )
        # +31s (61s since the first acme hit) — past the window, logs again.
        clock["t"] += 31.0
        await drive(
            middleware, captured, "/api/admin/upstreams/up-1",
            cookie_header=cookie, org_slug_header="acme",
        )

    access = [e for e in logs if e["event"] == "superadmin.org_access"]
    assert [e["org_slug"] for e in access] == ["acme", "beta", "acme"]


# ── Cloud mode: bare /mcp/ and /admin-mcp/ (current behaviour) ──────


@pytest.mark.asyncio
async def test_cloud_bare_mcp_uses_multi_org_sentinel() -> None:
    """Bare ``/mcp/`` in cloud is the *fixed* user MCP URL — middleware
    sets the multi-org sentinel and the gateway aggregates. (The old
    behavior 401-ed because the URL needed a slug; with a fixed URL
    that's no longer required.)"""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    settings = make_settings(mode="cloud")

    result = await run_middleware(settings, "/mcp/")

    assert result.status == 200
    assert result.downstream_path == "/mcp/"
    assert result.org_id == MULTI_ORG_SENTINEL
    assert result.org_slug == ""


@pytest.mark.asyncio
async def test_cloud_bare_admin_mcp_path_returns_401() -> None:
    settings = make_settings(mode="cloud")

    result = await run_middleware(settings, "/admin-mcp/")

    assert result.status == 401
    assert result.downstream_path is None


@pytest.mark.asyncio
async def test_cloud_bare_mcp_no_trailing_slash_uses_multi_org_sentinel() -> None:
    """``/mcp`` (no trailing slash) is what every MCP streamable-HTTP
    client actually POSTs to in cloud mode — the SDK's mount path is
    ``/mcp`` exactly, and there's no ASGI redirect for missing trailing
    slashes on POST. Treating that as ``DEFAULT_ORG_ID`` silently hands
    the user an empty tools list. The middleware must accept both
    ``/mcp`` and ``/mcp/...`` as multi-org requests."""
    from mcpolis.domain.ports import MULTI_ORG_SENTINEL

    settings = make_settings(mode="cloud")

    result = await run_middleware(settings, "/mcp")

    assert result.status == 200
    assert result.org_id == MULTI_ORG_SENTINEL


@pytest.mark.asyncio
async def test_cloud_reserved_segment_still_routes_to_default() -> None:
    """Phase 2 only tightens BARE /mcp/. Reserved segments (OAuth
    callbacks, well-known metadata) still fall through to the default
    org context — they serve their own mounts and aren't org-scoped."""
    settings = make_settings(mode="cloud")

    result = await run_middleware(settings, "/mcp/oauth/google/callback")

    assert result.status == 200
    assert result.downstream_path == "/mcp/oauth/google/callback"
    assert result.org_id == DEFAULT_ORG_ID


# ── Non-HTTP scope passthrough ───────────────────────────────────────


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_unchanged() -> None:
    """Middleware only acts on http scopes; lifespan/websocket pass
    through without touching contextvars."""
    settings = make_settings(mode="cloud")
    slug_cache = SlugCache()
    org_svc = make_org_service()
    called = False

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True

    middleware = OrgContextMiddleware(
        downstream, settings, org_svc,  # type: ignore[arg-type]
        slug_cache,
    )

    async def send(event: Message) -> None: ...
    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    await middleware({"type": "lifespan"}, receive, send)
    assert called


# ── structlog contextvar binding contract ────────────────────────────


@pytest.mark.asyncio
async def test_resolved_org_id_is_bound_to_structlog_contextvars() -> None:
    """Plain ``current_org_id.get()`` is read by application code; the
    structlog contextvars binding is what enriches *log records* with
    ``org_id`` — including stdlib records from the MCP SDK / httpx /
    uvicorn.access routed through ``ProcessorFormatter``'s
    ``foreign_pre_chain``. Without this binding, gateway access logs
    reach Elastic context-free even on resolved-slug requests. The
    /api/health line in prod proves the bridge already works end-to-
    end; this regression-guards the bind call itself."""
    import structlog
    from structlog.contextvars import clear_contextvars, get_contextvars

    settings = make_settings(mode="cloud")
    slug_cache = SlugCache()
    org_svc = make_org_service({"acme": "org-acme-123"})

    captured: dict[str, Any] = {}

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        captured["structlog_ctx"] = dict(get_contextvars())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({
            "type": "http.response.body", "body": b"", "more_body": False,
        })

    middleware = OrgContextMiddleware(
        downstream, settings, org_svc,  # type: ignore[arg-type]
        slug_cache,
    )

    scope: Scope = {
        "type": "http",
        "path": "/mcp/acme/",
        "raw_path": b"/mcp/acme/",
        "headers": [],
        "method": "GET",
    }

    async def send(event: Message) -> None: ...
    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    # Start from a clean structlog context so we measure only what
    # the middleware itself bound.
    clear_contextvars()
    try:
        # Bind a sentinel so we'd notice if the middleware accidentally
        # wiped pre-existing context (it doesn't, and shouldn't).
        structlog.contextvars.bind_contextvars(_sentinel="kept")
        await middleware(scope, receive, send)
    finally:
        clear_contextvars()

    bound = captured.get("structlog_ctx", {})
    assert bound.get("org_id") == "org-acme-123", (
        f"middleware must bind resolved org_id into structlog "
        f"contextvars; saw: {bound}"
    )
    # Pre-existing context untouched.
    assert bound.get("_sentinel") == "kept", (
        f"middleware must not wipe pre-existing structlog context; "
        f"saw: {bound}"
    )


@pytest.mark.asyncio
async def test_non_latin_mcp_path_does_not_crash() -> None:
    """Sibling of the snoopier host-rewrite crash in argus
    (ARGUS-BACKEND-6): the slug rewrite rebuilt ``raw_path`` by
    encoding the percent-DECODED path as latin-1, which raises
    UnicodeEncodeError for any non-Latin path a scanner probes and
    turns a clean 4xx into a 500."""
    settings = make_settings(mode="standalone")

    result = await run_middleware(settings, "/mcp/\u5e73\u4eee\u540d")

    assert result.status == 200
    assert result.downstream_path == "/mcp/\u5e73\u4eee\u540d"
