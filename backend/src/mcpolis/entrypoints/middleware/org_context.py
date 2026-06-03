"""Org context ASGI middleware — one code path for both modes.

Responsibilities
----------------
1. Recognise ``/admin-mcp/{slug}/...`` paths, resolve ``{slug}`` to an
   ``org_id``, rewrite the path so it falls through to the
   ``/admin-mcp`` mount, and set ``current_org_id`` for the downstream
   request.
2. Recognise gateway MCP paths in **cloud** mode:
   - ``/mcp`` and ``/mcp/`` → ``current_org_id = MULTI_ORG_SENTINEL``
     (merge mode — gateway controller fans out across the
     authenticated user's memberships). Kept for code; not surfaced in
     the product UI.
   - ``/mcp/{slug}/...`` → resolve ``{slug}``, rewrite the path to
     ``/mcp/...`` and set ``current_org_id`` to the resolved org.
     Tools are scoped to that org only.
3. For dashboard ``/api/*`` requests, read ``org_slug`` out of the
   session cookie and set ``current_org_id`` from it.
4. Anti-enumeration: an unknown slug in ``/admin-mcp/{slug}/...`` or
   ``/mcp/{slug}/...`` returns a 401 with the same body as
   "not authorized for this org".

In **standalone** mode the app has one org whose slug is ``default``.
External URLs stay slug-less; this middleware transparently injects
``/default`` into ``/mcp/...`` and ``/admin-mcp/...`` paths before the
slug resolver runs, so downstream only ever sees one shape. Reserved
segments (OAuth discovery, DCR, ``/admin-mcp/system``, ...) are left
alone because they serve their own mounts that aren't org-scoped.

Slug → org_id lookups are cached on the middleware instance to avoid a
Mongo round-trip per request. The cache is:

- populated lazily on first lookup;
- invalidated when an org is created (the org routes call
  ``OrgContextMiddleware.invalidate`` after each create);
- capped implicitly by the number of orgs in the system (no eviction —
  multi-tenant SaaS instances are bounded in org count).
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qsl

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcpolis.domain.model.reserved_slugs import RESERVED_MCP_SEGMENTS
from mcpolis.domain.ports import DEFAULT_ORG_ID, MULTI_ORG_SENTINEL
from mcpolis.domain.services.org_service import OrgNotFoundError, OrgService
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_org_slug,
)
from mcpolis.entrypoints.routes.dashboard_auth import (
    COOKIE_NAME,
    get_session_payload,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MCP_PREFIX = "/mcp/"
_ADMIN_MCP_PREFIX = "/admin-mcp/"

# ``RESERVED_MCP_SEGMENTS`` is imported at module top. Deliberately
# narrower than ``RESERVED_ORG_SLUGS``: that broader set (checked by
# the slug validator) also forbids things like ``default``, which is
# a legitimate slug at runtime (the built-in default org's slug).


class SlugCache:
    """Shared slug→org_id cache.

    A tiny shared-state object so that the org routes can invalidate
    the cache without holding a reference to the (lazily-instantiated)
    ASGI middleware. ``create_app`` constructs one and hands it to
    both the middleware and ``create_org_router``.
    """

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def get(self, slug: str) -> str | None:
        return self._map.get(slug)

    def put(self, slug: str, org_id: str) -> None:
        self._map[slug] = org_id

    def invalidate(self) -> None:
        self._map.clear()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock


class OrgContextMiddleware:
    """ASGI middleware that resolves ``current_org_id`` for each request.

    Must be added AFTER the rate-limit middleware in the ASGI stack so
    rate limiting sees the correct org key. In FastAPI, ``add_middleware``
    stacks are LIFO — the last one added runs first. This middleware
    is added **last** in ``create_app`` so that it runs first.
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        org_service: OrgService,
        slug_cache: SlugCache,
    ) -> None:
        self._app = app
        self._settings = settings
        self._org_service = org_service
        self._cache = slug_cache

    async def _resolve_slug(self, slug: str) -> str | None:
        cached = self._cache.get(slug)
        if cached is not None:
            return cached
        async with self._cache.lock:
            cached = self._cache.get(slug)
            if cached is not None:
                return cached
            try:
                org = await self._org_service.resolve_slug(slug)
            except OrgNotFoundError:
                return None
            self._cache.put(slug, org.id)
            return org.id

    @staticmethod
    def _split_slug_prefix(path: str, prefix: str) -> tuple[str, str] | None:
        """Return ``(slug, remaining_path)`` if ``path`` begins with
        ``prefix`` and has a slug segment after it, else ``None``.

        For ``/admin-mcp/acme/foo`` with prefix ``/admin-mcp/``:
        → ``("acme", "/admin-mcp/foo")``

        For ``/admin-mcp/acme`` (no trailing slash, just the slug):
        → ``("acme", "/admin-mcp/")``
        """
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        if not rest:
            return None
        slug, _, after = rest.partition("/")
        if not slug:
            return None
        base = prefix.rstrip("/")
        remaining = f"{base}/{after}" if after else f"{base}/"
        return slug, remaining

    @staticmethod
    def _is_reserved_mcp_request(path: str) -> bool:
        """True for ``/mcp/...`` paths that target an OAuth / discovery
        mount and must not be routed through the multi-org pipeline.
        """
        if not path.startswith(_MCP_PREFIX):
            return False
        rest = path[len(_MCP_PREFIX):]
        if not rest:
            return False
        first_segment = rest.split("/", 1)[0]
        return first_segment in RESERVED_MCP_SEGMENTS

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        response = JSONResponse(
            {"error": "Not authorized for this org"},
            status_code=401,
        )
        await response(scope, receive, send)

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Standalone mode: the app has one org whose slug is "default".
        # Keep external URLs slug-less by injecting "/default" into
        # /mcp/... and /admin-mcp/... paths here — the rest of the
        # middleware (slug resolution, downstream rewriting) then runs
        # the same code path as cloud admin-mcp. Reserved segments
        # (/mcp/.well-known, /mcp/oauth, etc.) are left alone because
        # they serve their own mounts that aren't org-scoped.
        if self._settings.mode == "standalone":
            path = _inject_default_slug(path)

        resolved_org_id: str | None = None
        resolved_slug: str = ""
        rewritten_path: str | None = None

        # 1. Cloud-mode user MCP request.
        #
        # Two shapes are accepted:
        #   - ``/mcp`` and ``/mcp/`` → MULTI_ORG_SENTINEL (merge mode).
        #     The gateway controller fans out across every org the
        #     authenticated user belongs to. Kept for code; deliberately
        #     not surfaced in the product UI.
        #   - ``/mcp/{slug}/...`` → resolve {slug}, rewrite to /mcp/...
        #     and pin ``current_org_id`` to that org. Tools are scoped
        #     to that single org. This is the URL surfaced in the UI
        #     and docs.
        #
        # Reserved paths under /mcp (OAuth, .well-known, ...) skip both
        # branches — they serve their own org-agnostic mounts.
        # Matching ``/mcp`` (no trailing slash) matters because the
        # streamable-HTTP transport mounts at ``/mcp`` exactly; a check
        # gated on the trailing slash would silently route the bare
        # path through DEFAULT_ORG_ID and return zero tools.
        if (
            self._settings.mode == "cloud"
            and (path == "/mcp" or path.startswith(_MCP_PREFIX))
            and not self._is_reserved_mcp_request(path)
        ):
            split = self._split_slug_prefix(path, _MCP_PREFIX)
            if split is None:
                # Bare ``/mcp`` or ``/mcp/`` — merge mode.
                resolved_org_id = MULTI_ORG_SENTINEL
            else:
                slug, new_path = split
                resolved_org_id = await self._resolve_slug(slug)
                if resolved_org_id is None:
                    await self._reject(scope, receive, send)
                    return
                resolved_slug = slug
                rewritten_path = new_path

        # 2. Admin MCP slug prefix (always slug-scoped). Standalone
        # /mcp/{slug} also lands here because of the default-slug
        # injection above.
        if resolved_org_id is None:
            split = self._split_slug_prefix(path, _ADMIN_MCP_PREFIX)
            if split is None and self._settings.mode == "standalone":
                # Standalone-only: /mcp/{default}/... post-injection.
                split = self._split_slug_prefix(path, _MCP_PREFIX)
            if split is not None:
                slug, new_path = split
                if slug in RESERVED_MCP_SEGMENTS:
                    # Not a slug — regular mount path under /admin-mcp
                    # (e.g. /admin-mcp/system). Leave the request alone.
                    pass
                else:
                    resolved_org_id = await self._resolve_slug(slug)
                    if resolved_org_id is None:
                        await self._reject(scope, receive, send)
                        return
                    resolved_slug = slug
                    rewritten_path = new_path

        # 3. Session cookie for dashboard /api/* requests.
        #
        # The cookie's ``org_slug`` claim is the default org context.
        # A super-admin (verified from that same signed cookie) may
        # override it per-request via an ``X-Org-Slug`` header or an
        # ``org`` query param, so the cross-org dashboard can drill
        # into any org's admin surface without rotating their session
        # cookie. EventSource can't set request headers, so the query
        # param is what scopes the SSE streams (/api/events,
        # /api/admin/upstreams/{id}/logs/stream). The override is gated
        # strictly on super-admin identity — a normal user setting the
        # header is ignored, so it grants no access they didn't have.
        if resolved_org_id is None and path.startswith("/api/"):
            cookie_header = ""
            header_override = ""
            for header_name, header_value in scope.get("headers", []):
                if header_name == b"cookie":
                    cookie_header = header_value.decode("latin-1")
                elif header_name == b"x-org-slug":
                    header_override = header_value.decode("latin-1").strip()
            payload = _extract_cookie_payload(cookie_header, self._settings)
            chosen_slug = _payload_str(payload, "org_slug")
            override = header_override or _query_param(scope, "org")
            if override:
                email = _payload_str(payload, "email")
                if email and email in self._settings.parsed_superadmin_emails():
                    chosen_slug = override
            if chosen_slug:
                resolved_org_id = await self._resolve_slug(chosen_slug)
                if resolved_org_id is not None:
                    resolved_slug = chosen_slug

        if resolved_org_id is None:
            # Bare ``/admin-mcp`` in cloud is a client error — it can't
            # be right in a multi-tenant deployment. Reserved prefixes
            # like ``/mcp/oauth`` or ``/mcp/.well-known`` fall through
            # here too (they skip slug resolution by design) but we let
            # them land in the default-org context because they serve
            # their own mounts.
            if (
                self._settings.mode == "cloud"
                and path.rstrip("/") == "/admin-mcp"
            ):
                await self._reject(scope, receive, send)
                return
            resolved_org_id = DEFAULT_ORG_ID

        token = current_org_id.set(resolved_org_id)
        slug_token = current_org_slug.set(resolved_slug)
        # Bind org_id into structlog contextvars too so that even
        # requests that get rejected upstream (e.g. /mcp/<slug>/...
        # returning 401 from AuthenticationMiddleware before reaching
        # our gateway log-context binder) still emit access logs
        # carrying ``org_id`` — without this, the unauthenticated tail
        # of gateway traffic is context-free in Elastic. Async-task
        # scoping isolates this binding per request; matches the no-
        # unbind pattern in ``user_identity_middleware``.
        structlog.contextvars.bind_contextvars(org_id=resolved_org_id)
        try:
            if rewritten_path is not None:
                # Mutate a shallow copy of the scope so the downstream
                # mount routes the request to the non-slugged prefix.
                new_scope: dict[str, Any] = dict(scope)
                new_scope["path"] = rewritten_path
                new_scope["raw_path"] = rewritten_path.encode("latin-1")
                await self._app(new_scope, receive, send)
            else:
                await self._app(scope, receive, send)
        finally:
            current_org_id.reset(token)
            current_org_slug.reset(slug_token)


def _inject_default_slug(path: str) -> str:
    """Prepend ``default`` to ``/mcp/...`` and ``/admin-mcp/...`` paths
    so standalone requests go through the same slug-aware pipeline as
    cloud admin-mcp. No-op for reserved segments
    (``/mcp/.well-known``, ``/mcp/oauth``, etc.) and for paths that
    don't target one of the org-scoped mounts.

    Examples (standalone, mode only):
    - ``/mcp/my-tools`` → ``/mcp/default/my-tools``
    - ``/mcp/`` → ``/mcp/default/``
    - ``/mcp`` → ``/mcp/default/``
    - ``/mcp/.well-known/foo`` → unchanged
    - ``/admin-mcp/users`` → ``/admin-mcp/default/users``
    - ``/admin-mcp/system/foo`` → unchanged
    - ``/api/auth/status`` → unchanged
    """
    for prefix in ("/mcp", "/admin-mcp"):
        if path == prefix or path == prefix + "/":
            return f"{prefix}/default/"
        if path.startswith(prefix + "/"):
            rest = path[len(prefix) + 1:]
            first_segment = rest.split("/", 1)[0]
            if first_segment in RESERVED_MCP_SEGMENTS:
                return path
            return f"{prefix}/default/{rest}"
    return path


def _extract_cookie_payload(
    cookie_header: str, settings: Settings,
) -> dict[str, Any] | None:
    """Decode + verify the signed session cookie from a Cookie header.

    Returns the payload dict (carrying ``email`` and ``org_slug``) or
    ``None`` when there's no valid mcpolis session cookie. The HMAC
    signature is validated by ``get_session_payload`` — an attacker
    can't spoof identity or org by crafting a raw cookie.
    """
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            return get_session_payload(settings, value)
    return None


def _payload_str(payload: dict[str, Any] | None, key: str) -> str:
    """Read a string claim from a decoded cookie payload, else ``""``."""
    if payload is None:
        return ""
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _query_param(scope: Scope, name: str) -> str:
    """Return the first value of query param ``name``, else ``""``."""
    raw = scope.get("query_string", b"")
    if not raw:
        return ""
    for key, value in parse_qsl(raw.decode("latin-1")):
        if key == name:
            return value.strip()
    return ""
