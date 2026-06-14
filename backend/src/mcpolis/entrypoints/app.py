# pyright: reportUnusedFunction=false
from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Request, Response
from structlog.contextvars import (
    bind_contextvars,
    bound_contextvars,
    clear_contextvars,
)
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.sandbox_service import (
    SandboxProviderName,
    SandboxService,
)
from mcpolis.adapters.observability.analytics_client import (
    AnalyticsClient,
    build_analytics_client,
    set_analytics,
)
from mcpolis.adapters.observability.sentry_setup import init_sentry
from mcpolis.adapters.observability.structlog_setup import configure_structlog
from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.adapters.repositories.audit_repository import (
    AuditRepository as LegacyAuditRepository,
)
from mcpolis.adapters.repositories.oauth_apps_loader import load_oauth_apps
from mcpolis.adapters.repositories.upstream_config_loader import load_merged_config
from mcpolis.dev.demo_mcp_server import (
    DEMO_UPSTREAM_DISPLAY_NAME,
    build_demo_app,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.service_token import SVC_IDENTITY_PREFIX
from mcpolis.domain.model.settings import OAuthAppsConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.dashboard_oauth_provider import DashboardOAuthProvider
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.oauth_state_repository import OAuthStateRepository
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.plan_policy import PlanLimitExceeded
from mcpolis.domain.services.org_service import OrgService as OrgServiceCls
from mcpolis.domain.services.policy_notifier import PolicyNotifier
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.storage_factory import (
    StorageBundle,
    build_storage,
    initialize_storage,
)
from mcpolis.entrypoints.config import (
    Settings,
    effective_dashboard_provider,
    effective_gateway_url,
    validate_startup_secrets,
)
from mcpolis.entrypoints.lifecycle import DrainCoordinator
from mcpolis.entrypoints.controllers.admin_mcp_controller import create_admin_mcp_server
from mcpolis.entrypoints.controllers.gateway_controller import (
    create_mcp_server,
    current_org_id,
    current_session_id,
    current_user_id,
)
from mcpolis.entrypoints.routes.dashboard_api import (
    StartupStatusResponse,
    create_dashboard_api_router,
)
from mcpolis.entrypoints.routes.dashboard_auth import (
    COOKIE_NAME as SESSION_COOKIE_NAME,
    create_dashboard_auth,
    get_session_payload,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)



class _GatewayLogContextBindMiddleware:
    """Bind request-scoped log context for mounted MCP / admin-mcp /
    superadmin sub-apps so every log line emitted during the request —
    structlog from our code, and stdlib lines from the SDK / httpx /
    uvicorn.access routed through ``ProcessorFormatter``'s
    ``foreign_pre_chain`` — carries ``user_id`` / ``session_id`` /
    ``org_id``.

    The mounted gateway sub-apps bypass the FastAPI ``@app.middleware``
    decorators (``log_context_middleware`` / ``user_identity_middleware``)
    that bind these for ``/api/*`` traffic. Without this middleware,
    ``/mcp/<slug>/...`` access logs reach Elastic context-free — exactly
    the gap that made the MCPOLIS-BACKEND-C investigation harder than it
    needed to be (the SDK's ``mcp.client.auth.oauth2`` ERROR records had
    no upstream/user binding).

    Installed *after* ``AuthContextMiddleware`` so ``auth_context_var``
    is populated when this runs. Plain ``bind_contextvars`` (no later
    unbind) matches the pattern used by ``user_identity_middleware`` —
    contextvars are async-task-scoped, so per-request isolation is
    handled by asyncio Task ``Context`` copies; no cross-request leak.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from mcp.server.auth.middleware.auth_context import auth_context_var

        request = Request(scope)
        session_id = request.headers.get("mcp-session-id")
        auth_user = auth_context_var.get(None)
        user_id = (
            auth_user.display_name if auth_user is not None else "anonymous"
        )
        try:
            org_id = current_org_id.get()
        except LookupError:
            org_id = ""
        bind_contextvars(
            user_id=user_id,
            session_id=session_id,
            org_id=org_id,
        )
        await self.app(scope, receive, send)


class _SessionRegistrationMiddleware:
    """ASGI middleware that registers MCP session → user mapping.

    Also returns 404 (instead of the library's 400) for stale session IDs
    so that MCP clients know to re-initialise.
    """

    def __init__(
        self,
        app: ASGIApp,
        session_registry: GatewaySessionRegistry,
        session_manager: StreamableHTTPSessionManager,
        audit_repo: LegacyAuditRepository | None = None,
    ) -> None:
        self.app = app
        self._registry = session_registry
        self._session_manager = session_manager
        self._audit_repo = audit_repo

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            from starlette.requests import Request as StarletteRequest
            from starlette.responses import Response as StarletteResponse

            request = StarletteRequest(scope)
            session_id = request.headers.get("mcp-session-id")

            # Workaround for modelcontextprotocol/python-sdk#1727:
            # StreamableHTTPSessionManager returns 400 for unknown session IDs,
            # but the MCP spec requires 404 so clients know to re-initialise.
            # Without this, clients (e.g. Claude Code) get stuck after a server
            # restart because they keep replaying the stale session ID.
            if (
                session_id is not None
                and session_id not in self._session_manager._server_instances  # pyright: ignore[reportPrivateUsage]
            ):
                logger.info(
                    "session.stale.rejected",
                    session_id_prefix=session_id[:8],
                )
                response = StarletteResponse(
                    "Not Found: Session has been terminated",
                    status_code=404,
                )
                await response(scope, receive, send)
                return

            if session_id is not None:
                # User is set by the OAuth auth middleware above us.
                # ``anonymous`` only happens when something earlier in
                # the stack mounted this without auth — shouldn't occur
                # post-Phase D, but kept as a defensive fallback so the
                # session registry can still record the row.
                from mcp.server.auth.middleware.auth_context import auth_context_var
                auth_user = auth_context_var.get(None)
                user_id = auth_user.display_name if auth_user is not None else "anonymous"

                is_new = not self._registry.has_session(session_id)
                org_id = current_org_id.get()
                self._registry.register(session_id, org_id, user_id)

                if is_new and self._audit_repo is not None:
                    from datetime import UTC, datetime
                    from mcpolis.domain.model.audit import AuditEntry, parse_client_type

                    user_agent = request.headers.get("user-agent")
                    client_type = parse_client_type(user_agent)
                    entry = AuditEntry(
                        timestamp=datetime.now(UTC).isoformat(),
                        action="client_connect",
                        user_id=user_id,
                        upstream_id="",
                        session_id=session_id,
                        client_type=client_type,
                        outcome="success",
                    )
                    await self._audit_repo.log(org_id, entry)
        await self.app(scope, receive, send)


class _MultiOrgUserOrgsPrefetchMiddleware:
    """ASGI middleware that pre-resolves the authenticated user's orgs.

    Runs after ``AuthContextMiddleware`` so ``auth_context_var`` is set;
    only fires for cloud multi-org gateway requests
    (``current_org_id == MULTI_ORG_SENTINEL``). Stashes the resolved
    list on ``current_user_orgs`` so the MCP SDK's sync
    ``Server.create_initialization_options`` callback can pick a
    per-session ``instructions`` string that names the user's
    organizations without having to ``await``.

    One Mongo round-trip per session-init / per request — cheap, and
    keeps the personalization fully accurate (no stale per-process
    caches to invalidate when memberships change).
    """

    def __init__(
        self,
        app: ASGIApp,
        org_service: OrgServiceCls,
    ) -> None:
        self.app = app
        self._org_service = org_service

    async def __call__(
        self, scope: Any, receive: Any, send: Any,
    ) -> None:
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcpolis.domain.ports import MULTI_ORG_SENTINEL
        from mcpolis.entrypoints.controllers.gateway_controller import (
            current_org_id as _current_org_id,
            current_user_orgs,
        )

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            org_id = _current_org_id.get()
        except LookupError:
            org_id = ""
        if org_id != MULTI_ORG_SENTINEL:
            await self.app(scope, receive, send)
            return
        auth_user = auth_context_var.get(None)
        if auth_user is None:
            await self.app(scope, receive, send)
            return
        try:
            orgs = await self._org_service.list_user_orgs(auth_user.display_name)
        except Exception:
            logger.exception(
                "gateway.user_orgs.prefetch.failed",
                user=auth_user.display_name,
            )
            orgs = []
        token = current_user_orgs.set(orgs)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_orgs.reset(token)


def _build_mcp_app_with_oauth(
    session_manager: StreamableHTTPSessionManager,
    settings: Settings,
    runtime_manager: OrgRuntimeManager,
    oauth_state_repo: OAuthStateRepository,
    org_service: OrgServiceCls,
    service_token_service: ServiceTokenService,
    event_bus: EventStream | None = None,
    session_registry: GatewaySessionRegistry | None = None,
    audit_repo: LegacyAuditRepository | None = None,
) -> Starlette:
    """Build the MCP Starlette app with OAuth auth routes and middleware."""
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
    from mcp.server.auth.routes import create_auth_routes
    from pydantic import AnyHttpUrl
    from starlette.middleware.authentication import AuthenticationMiddleware

    from mcpolis.adapters.auth.mcp_gateway_oauth_provider import McpGatewayOAuthProvider
    from mcpolis.adapters.auth.service_token_verifier import (
        CompositeGatewayTokenVerifier,
        ServiceTokenVerifier,
    )
    from mcpolis.entrypoints.middleware.service_token_pin import (
        ServiceTokenOrgPinMiddleware,
    )
    from mcpolis.entrypoints.routes.google_callback import create_google_callback_route

    # Gateway OAuth issuer + Google callback live under the gateway's
    # public base URL — which may be a subdomain (``mcp.mcphero.io``)
    # while the dashboard stays on apex. ``effective_gateway_url``
    # falls back to ``server_url`` for single-origin deploys.
    gateway_url = effective_gateway_url(settings)
    issuer_url = AnyHttpUrl(f"{gateway_url}/mcp")

    # Create the OAuth provider
    provider = McpGatewayOAuthProvider(
        google_client_id=settings.google_client_id,
        google_client_secret=settings.google_client_secret,
        server_url=gateway_url,
        runtime_manager=runtime_manager,
        state_repository=oauth_state_repo,
        event_bus=event_bus,
    )

    raw_mcp_app: ASGIApp = StreamableHTTPASGIApp(session_manager)
    if session_registry is not None:
        raw_mcp_app = _SessionRegistrationMiddleware(
            raw_mcp_app, session_registry, session_manager, audit_repo=audit_repo,
        )

    # Auth middleware. The composite verifier dispatches svct_-prefixed
    # bearers to the service-token registry and everything else to the
    # OAuth provider; the OAuth routes below keep the raw provider.
    middleware = [
        Middleware(
            AuthenticationMiddleware,
            backend=BearerAuthBackend(
                CompositeGatewayTokenVerifier(
                    ServiceTokenVerifier(service_token_service),
                    provider,
                ),
            ),
        ),
        Middleware(AuthContextMiddleware),
        # Org pinning for service tokens. Ordering is load-bearing:
        # after AuthContextMiddleware (reads auth_context_var), before
        # the log binder (logs must carry the pinned org, not the
        # multi-org sentinel) and before the orgs-prefetch middleware
        # (list_user_orgs must never run for ``svc:`` identities).
        Middleware(ServiceTokenOrgPinMiddleware),
        # Bind ``user_id`` / ``session_id`` / ``org_id`` into structlog
        # contextvars so every log line emitted during the request —
        # including stdlib SDK / httpx / uvicorn.access — carries them.
        # The mounted sub-app bypasses the FastAPI ``@app.middleware``
        # decorators that do this for ``/api/*``; without this, gateway
        # access logs reach Elastic with no business context.
        Middleware(_GatewayLogContextBindMiddleware),
        # Runs after AuthContextMiddleware so it can read auth_context_var
        # and do an async lookup of the user's orgs before the sync MCP
        # init_options callback fires.
        Middleware(
            _MultiOrgUserOrgsPrefetchMiddleware,
            org_service=org_service,
        ),
    ]

    # Build routes
    routes: list[Route] = []

    # OAuth endpoints (metadata, authorize, token, register)
    from mcp.server.auth.settings import ClientRegistrationOptions

    routes.extend(
        create_auth_routes(
            provider=provider,
            issuer_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
    )

    # Protected resource metadata (tells clients where the auth server
    # is). Slug-aware: in cloud mode the handler reads the current org
    # slug from the request context and advertises
    # ``<server>/mcp/<slug>`` so Claude registers against the correct
    # per-org resource. In standalone mode the slug is empty and it
    # reduces to the SDK's static behavior.
    from mcp.server.auth.routes import cors_middleware

    from mcpolis.entrypoints.slug_aware_oauth import (
        SlugAwareProtectedResourceMetadataHandler,
        SlugAwareRequireAuthMiddleware,
    )

    mcp_base_url = f"{gateway_url}/mcp"
    protected_metadata_handler = SlugAwareProtectedResourceMetadataHandler(
        base_url=mcp_base_url,
        authorization_servers=[issuer_url],
    )
    routes.append(
        Route(
            "/.well-known/oauth-protected-resource",
            endpoint=cors_middleware(
                protected_metadata_handler.handle,
                ["GET", "OPTIONS"],
            ),
            methods=["GET", "OPTIONS"],
        ),
    )

    # Google callback route
    callback_route = create_google_callback_route()
    routes.append(callback_route)

    # MCP endpoint wrapped with slug-aware bearer middleware so the 401
    # ``WWW-Authenticate`` header points at the slug-scoped metadata URL.
    routes.append(
        Route(
            "/",
            endpoint=SlugAwareRequireAuthMiddleware(
                raw_mcp_app, [], mcp_base_url,
            ),
            methods=["GET", "POST", "DELETE"],
        ),
    )

    mcp_app = Starlette(
        routes=routes,
        middleware=middleware,
    )
    # Store the provider on the app so the callback route can access it
    mcp_app.state.mcp_gateway_oauth_provider = provider  # type: ignore[attr-defined]

    return mcp_app


def _build_admin_app_with_oauth(
    admin_mcp_starlette_app: ASGIApp,
    provider: Any,
    settings: Settings,
    runtime_manager: OrgRuntimeManager,
) -> Starlette:
    """Build admin MCP Starlette app with same OAuth as the gateway."""
    from mcp.server.auth.middleware.auth_context import (
        AuthContextMiddleware,
        auth_context_var,
    )
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
    from mcp.server.auth.routes import create_auth_routes
    from pydantic import AnyHttpUrl
    from starlette.middleware.authentication import AuthenticationMiddleware

    server_url = settings.server_url.rstrip("/")
    issuer_url = AnyHttpUrl(f"{server_url}/admin-mcp")

    middleware = [
        Middleware(
            AuthenticationMiddleware,
            backend=BearerAuthBackend(provider),
        ),
        Middleware(AuthContextMiddleware),
        # See ``_GatewayLogContextBindMiddleware`` docstring — binds
        # user/session/org for log enrichment on this mounted sub-app.
        Middleware(_GatewayLogContextBindMiddleware),
    ]

    routes: list[Route] = []

    from mcp.server.auth.settings import ClientRegistrationOptions

    routes.extend(
        create_auth_routes(
            provider=provider,
            issuer_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True
            ),
        )
    )

    # Slug-aware metadata handler — same rationale as the gateway app.
    from mcp.server.auth.routes import cors_middleware

    from mcpolis.entrypoints.slug_aware_oauth import (
        SlugAwareProtectedResourceMetadataHandler,
        SlugAwareRequireAuthMiddleware,
    )

    admin_base_url = f"{server_url}/admin-mcp"
    admin_metadata_handler = SlugAwareProtectedResourceMetadataHandler(
        base_url=admin_base_url,
        authorization_servers=[issuer_url],
    )
    routes.append(
        Route(
            "/.well-known/oauth-protected-resource",
            endpoint=cors_middleware(
                admin_metadata_handler.handle,
                ["GET", "OPTIONS"],
            ),
            methods=["GET", "OPTIONS"],
        ),
    )

    # Admin role check wrapper
    async def admin_role_check(
        request: Request,
    ) -> Response | None:
        """Return a 403 response if the user doesn't have admin role.

        Post-OAuth-state-refactor (commit 725d175) gateway tokens are
        global and user-scoped — they no longer pin a tenant. This
        function is the structural cross-org isolation gate for the
        ``/admin-mcp/{slug}`` mount: ``current_org_id`` is set by
        ``OrgContextMiddleware`` from the URL slug, and
        ``is_admin(email)`` consults THAT org's policy. If the user
        isn't in the resolved org's ``config.users`` dict at all,
        ``is_admin`` returns False and we 403. Pinned by
        ``test_has_role_admin_is_per_org_isolation_gate`` in
        ``test_policy_engine.py``.
        """
        auth_user = auth_context_var.get(None)
        if auth_user is None:
            return JSONResponse(
                {"error": "Not authenticated"}, status_code=401
            )
        user_email = auth_user.display_name
        # Belt-and-braces: service tokens are structurally rejected on
        # /admin-mcp (this app wraps the raw OAuth provider, so a
        # svct_ bearer never authenticates), but guard explicitly in
        # case the verifier wiring ever changes.
        if user_email.startswith(SVC_IDENTITY_PREFIX):
            return JSONResponse(
                {"error": "Service tokens are not accepted on the admin MCP"},
                status_code=403,
            )
        org_id = current_org_id.get()
        org_runtime = await runtime_manager.get(org_id)
        if not org_runtime.policy_engine.is_admin(user_email):
            return JSONResponse(
                {"error": f"User '{user_email}' does not have admin role"},
                status_code=403,
            )
        return None

    from starlette.requests import Request as StarletteRequest

    class AdminRoleMiddleware:
        """ASGI middleware that checks admin role after OAuth auth."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(
            self, scope: dict[str, Any], receive: Any, send: Any
        ) -> None:
            if scope["type"] == "http":
                request = StarletteRequest(scope)
                denial = await admin_role_check(request)
                if denial is not None:
                    await denial(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    routes.append(
        Route(
            "/",
            endpoint=SlugAwareRequireAuthMiddleware(
                AdminRoleMiddleware(admin_mcp_starlette_app),
                [],
                admin_base_url,
            ),
            methods=["GET", "POST", "DELETE"],
        ),
    )

    from mcpolis.entrypoints.routes.google_callback import (
        create_google_callback_route,
    )
    routes.append(create_google_callback_route())

    admin_app = Starlette(routes=routes, middleware=middleware)
    admin_app.state.mcp_gateway_oauth_provider = provider  # type: ignore[attr-defined]
    return admin_app


def _build_superadmin_app_with_oauth(
    superadmin_mcp_app: ASGIApp,
    provider: Any,
    settings: Settings,
    allowed_emails: set[str],
) -> Starlette:
    """Build superadmin MCP app: same OAuth as gateway, email allowlist check."""
    from mcp.server.auth.middleware.auth_context import (
        AuthContextMiddleware,
        auth_context_var,
    )
    from mcp.server.auth.middleware.bearer_auth import (
        BearerAuthBackend,
        RequireAuthMiddleware,
    )
    from mcp.server.auth.routes import create_auth_routes
    from pydantic import AnyHttpUrl
    from starlette.middleware.authentication import AuthenticationMiddleware

    server_url = settings.server_url.rstrip("/")
    issuer_url = AnyHttpUrl(f"{server_url}/admin-mcp/system")

    middleware = [
        Middleware(
            AuthenticationMiddleware,
            backend=BearerAuthBackend(provider),
        ),
        Middleware(AuthContextMiddleware),
        # See ``_GatewayLogContextBindMiddleware`` docstring — binds
        # user/session/org for log enrichment on this mounted sub-app.
        Middleware(_GatewayLogContextBindMiddleware),
    ]

    routes: list[Route] = []

    from mcp.server.auth.settings import ClientRegistrationOptions

    routes.extend(
        create_auth_routes(
            provider=provider,
            issuer_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
    )

    from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
    from mcp.server.auth.routes import cors_middleware
    from mcp.shared.auth import ProtectedResourceMetadata

    resource_server_url = AnyHttpUrl(f"{server_url}/admin-mcp/system")
    protected_resource_metadata = ProtectedResourceMetadata(
        resource=resource_server_url,
        authorization_servers=[issuer_url],
    )
    routes.append(
        Route(
            "/.well-known/oauth-protected-resource",
            endpoint=cors_middleware(
                ProtectedResourceMetadataHandler(protected_resource_metadata).handle,
                ["GET", "OPTIONS"],
            ),
            methods=["GET", "OPTIONS"],
        ),
    )

    # Email allowlist check
    async def superadmin_check(request: Request) -> Response | None:
        auth_user = auth_context_var.get(None)
        if auth_user is None:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        if auth_user.display_name not in allowed_emails:
            return JSONResponse({"error": "Not a superadmin"}, status_code=403)
        return None

    from starlette.requests import Request as StarletteRequest

    class SuperadminEmailMiddleware:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                request = StarletteRequest(scope)
                denial = await superadmin_check(request)
                if denial is not None:
                    await denial(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    resource_metadata_url = AnyHttpUrl(
        f"{server_url}/admin-mcp/system/.well-known/oauth-protected-resource"
    )
    routes.append(
        Route(
            "/",
            endpoint=RequireAuthMiddleware(
                SuperadminEmailMiddleware(superadmin_mcp_app),
                [],
                resource_metadata_url,
            ),
            methods=["GET", "POST", "DELETE"],
        ),
    )

    from mcpolis.entrypoints.routes.google_callback import create_google_callback_route
    routes.append(create_google_callback_route())

    sa_app = Starlette(routes=routes, middleware=middleware)
    sa_app.state.mcp_gateway_oauth_provider = provider  # type: ignore[attr-defined]
    return sa_app


def _build_sandbox_provider_plumbing(
    settings: Settings,
    storage: StorageBundle,
) -> tuple[
    SandboxResolver,
    dict[SandboxProviderName, SandboxService],
    SandboxPersistenceRepository,
    str,
]:
    """Build the resolver + service registry + persistence + instance ID.

    Two providers, picked by ``MCPOLIS_SANDBOX_PROVIDER``:

    - ``e2b`` (default in cloud) — ``E2BSandboxService`` backed by
      ``RealE2BClient(api_key=settings.e2b_api_key)``.
    - ``local-subprocess`` (dev only; rejected in cloud by the
      startup validator) — ``LocalSubprocessSandboxService``.

    Empty ``MCPOLIS_SANDBOX_PROVIDER`` falls back to ``e2b`` when
    an API key is configured, else ``local-subprocess`` (dev).

    The ``mcpolis_instance`` UUID is minted per process so the
    E2B reconciler can distinguish "my sandboxes" from another
    instance's during multi-instance deploys (plan §"Resilience").
    """
    import uuid

    from mcpolis.adapters.sandbox_e2b import (  # noqa: PLC0415
        E2BSandboxService,
        RealE2BClient,
    )
    from mcpolis.adapters.sandbox_services import (  # noqa: PLC0415
        LocalSubprocessSandboxService,
    )

    instance_id = uuid.uuid4().hex
    logger.info("sandbox.instance_id.minted", mcpolis_instance=instance_id)

    persistence: SandboxPersistenceRepository = (
        storage.sandbox_persistence_repo
    )

    services: dict[SandboxProviderName, SandboxService] = {
        "local-subprocess": LocalSubprocessSandboxService(),
    }
    if settings.e2b_api_key:
        services["e2b"] = E2BSandboxService(
            RealE2BClient(api_key=settings.e2b_api_key),
            mcpolis_instance=instance_id,
            persistence=persistence,
            volumes_enabled=settings.e2b_volumes_enabled,
            on_timeout_seconds=settings.e2b_idle_pause_seconds,
            reuse_sandboxes_on_restart=settings.e2b_reuse_sandboxes_on_restart,
        )

    requested = settings.sandbox_provider.strip()
    if requested:
        provider: SandboxProviderName = requested  # type: ignore[assignment]
    else:
        provider = "e2b" if "e2b" in services else "local-subprocess"

    if provider not in services:
        raise RuntimeError(
            f"sandbox provider {provider!r} requested but not "
            f"buildable from current settings; have "
            f"{sorted(services)}",
        )

    if provider == "local-subprocess":
        # No-isolation dev path: stdio MCPs run as ordinary host
        # subprocesses (no CPU/RAM/disk enforcement, no egress filter,
        # full host filesystem and network). Cloud mode rejects this
        # provider outright in ``validate_startup_secrets``; here
        # (standalone / dev) we allow it but flag it loudly so the
        # operator knows the boundary is off. The README's "flagged as
        # unsafe at startup" promise is this line.
        logger.warning(
            "sandbox.provider.local_subprocess.unsafe",
            provider=provider,
            message=(
                "Sandbox provider resolved to 'local-subprocess': stdio "
                "MCPs run UNSANDBOXED as host subprocesses with no "
                "isolation (no resource limits, no egress filtering, "
                "full host access). This is a dev-only path. Set "
                "MCPOLIS_E2B_API_KEY (or MCPOLIS_SANDBOX_PROVIDER=e2b) to "
                "run stdio MCPs in an isolated E2B sandbox."
            ),
        )

    resolver = SandboxResolver(global_provider=provider)
    return resolver, services, persistence, instance_id


async def _run_sandbox_reconcile_at_boot(
    *,
    settings: Settings,
    storage: StorageBundle,
    sandbox_services: dict[SandboxProviderName, SandboxService] | None,
    mcpolis_instance: str,
    event_stream: EventStream,
) -> None:
    """Run the per-backend startup reconciler before traffic flows.

    Plan §"Resilience" point 4. Pre-conditions:

    - Active provider is ``e2b``. The own-runner reconciler is
      deferred (see ``internal/documents/stdio-mcp-sandboxing.md`` §9
      OPEN question).
    - Persistence is Mongo-backed. Running against an empty
      in-memory repository would treat every live sandbox as an
      orphan and kill them all; the plan explicitly forbids this.
    - The startup validator already passed, so credentials are
      present.

    Skip silently when any precondition fails — the reconciler is a
    durability optimisation, not a correctness gate. Failures inside
    ``reconcile()`` are logged + swallowed by the reconciler itself.
    """
    if settings.sandbox_provider != "e2b":
        return
    if sandbox_services is None or "e2b" not in sandbox_services:
        return

    # Standalone mode keeps refs in memory — there's no durable
    # source of truth to cross-reference against, so reconcile would
    # kill everything. Cloud mode flips to Mongo.
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (  # noqa: PLC0415
        InMemorySandboxPersistenceRepository,
    )

    if isinstance(
        storage.sandbox_persistence_repo, InMemorySandboxPersistenceRepository,
    ):
        logger.info(
            "sandbox.reconcile.skipped",
            reason="in-memory persistence — no durable refs to compare against",
        )
        return

    from mcpolis.adapters.sandbox_e2b import (  # noqa: PLC0415
        E2BSandboxReconciler,
        E2BSandboxService,
    )

    e2b_service = sandbox_services["e2b"]
    if not isinstance(e2b_service, E2BSandboxService):
        return  # operator wired a custom service; let them own its lifecycle

    reconciler = E2BSandboxReconciler(
        e2b_service._client,  # pyright: ignore[reportPrivateUsage]
        storage.sandbox_persistence_repo,
        mcpolis_instance=mcpolis_instance,
    )
    report = await reconciler.reconcile()
    # SSE event so the admin UI can show "boot reconcile: killed N
    # orphans, kept M paused snapshots, GC'd K stale snapshots."
    # Reconcile is a cross-org operator-level operation; publish
    # under DEFAULT_ORG_ID (the same sentinel the operator
    # denylist uses for its global state) rather than fanning out
    # to every org's stream.
    from mcpolis.domain.model.events import Event  # noqa: PLC0415
    from mcpolis.domain.ports import DEFAULT_ORG_ID  # noqa: PLC0415

    event_stream.publish(
        DEFAULT_ORG_ID,
        Event(
            type="sandbox_reconcile_report",
            payload={
                "provider": report.provider,
                "mcpolis_instance": report.mcpolis_instance,
                "killed_orphan_sandboxes": report.killed_orphan_sandboxes,
                "kept_paused_snapshots": report.kept_paused_snapshots,
                "gc_old_unknown_snapshots": report.gc_old_unknown_snapshots,
                "skipped_other_instance": report.skipped_other_instance,
            },
        ),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    # Fail fast on misconfigured deploys: in cloud mode, required secrets
    # must be present with non-dev values. Standalone mode is a no-op.
    validate_startup_secrets(settings)
    logger.info("app.startup", mode=settings.mode)

    # Initialize Sentry before any app construction so errors during
    # startup are captured too. No-op when MCPOLIS_SENTRY_DSN is empty.
    init_sentry(settings)

    # Mixpanel analytics client (fire-and-forget). No-op when
    # MCPOLIS_MIXPANEL_TOKEN is empty. Registered as a module-level
    # singleton so deep call sites can import it without plumbing.
    analytics: AnalyticsClient = build_analytics_client(settings)
    set_analytics(analytics)

    # Create config files from *.init.* templates if they don't exist
    for path in [settings.mcp_json_path, settings.config_path]:
        if not path.exists():
            init_name = path.stem + ".init" + path.suffix
            init_path = path.with_name(init_name)
            if init_path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(init_path, path)
                logger.info(
                    "config.file.initialized",
                    path=str(path),
                    source=str(init_path),
                )

    # Load instance-level OAuth app credentials (domain → client_id/secret)
    oauth_apps: OAuthAppsConfig = load_oauth_apps(
        inline_json=settings.oauth_apps_json,
        file_path=settings.oauth_apps_path,
    )

    # Storage factory — picks file or Mongo repositories + builds the
    # per-mode EventStream and RateLimiter.
    storage: StorageBundle = build_storage(settings, oauth_apps)
    event_bus: EventStream = storage.event_stream
    audit_repo = storage.audit_repo
    connection_store = storage.connection_repo
    policy_store = storage.config_repo

    # Sandbox-runner state registry + connection config (Phase H).
    # Built early so it can be threaded through the runtime manager
    # and back into ``UpstreamClientManager``. ``event_bus`` is already
    # constructed inside ``storage`` so the registry can publish
    # ``sandbox_state_changed`` SSE events from the moment any
    # runtime connects.
    # Sandbox provider boundary — resolver + service registry +
    # write-through persistence + multi-instance tag, all derived
    # from Settings.
    (
        sandbox_resolver,
        sandbox_services,
        sandbox_persistence,
        mcpolis_instance,
    ) = _build_sandbox_provider_plumbing(settings, storage)

    # Per-org runtime manager — replaces the old global singletons.
    runtime_manager = OrgRuntimeManager(
        config_repo=policy_store,
        upstream_config_repo=storage.upstream_config_repo,
        connection_repo=connection_store,
        audit_repo=audit_repo,
        tool_catalog_repo=storage.tool_catalog_repo,
        server_url=settings.server_url,
        sandbox_resolver=sandbox_resolver,
        sandbox_services=sandbox_services,
        sandbox_persistence=sandbox_persistence,
        mcpolis_instance=mcpolis_instance,
        template_var_repo=storage.template_var_repo,
        sandbox_file_repo=storage.sandbox_file_repo,
    )

    drain = DrainCoordinator(drain_timeout=settings.drain_timeout)

    # Standalone-mode: load config + upstreams from files synchronously
    # and create the default org runtime before the event loop starts.
    # Cloud mode: runtimes are created async during the lifespan.
    standalone_upstreams: list[UpstreamDefinition] | None = None
    if storage.file_mcp_store is not None:
        app_config = storage.config_repo.ensure_defaults_sync(DEFAULT_ORG_ID)
        upstream_options = {k: v.model_dump() for k, v in app_config.upstreams.items()}
        standalone_upstreams = load_merged_config(
            settings.mcp_json_path, upstream_options, oauth_apps=oauth_apps,
            allow_stdio=settings.allow_stdio_mcp,
        )
        runtime_manager.create_runtime_sync(
            DEFAULT_ORG_ID, app_config, standalone_upstreams,
        )
        # Standalone mode runs as a single org whose slug equals
        # DEFAULT_ORG_ID. The wrapped resource URI scheme bakes the
        # slug in, so register it here too.
        runtime_manager.register_slug(DEFAULT_ORG_ID, DEFAULT_ORG_ID)
        logger.info(
            "upstream.config.loaded",
            upstream_count=len(standalone_upstreams),
        )

        # Prune persisted data for users / upstreams that are no longer configured
        from mcpolis.domain.services.data_pruner import prune_data
        prune_data(
            DEFAULT_ORG_ID,
            settings.data_dir,
            valid_emails=set(app_config.users.keys()),
            valid_upstream_ids={u.id for u in standalone_upstreams},
        )
    else:
        logger.info("app.startup.cloud_mode_deferred_runtimes")

    # Upstream OAuth components
    from mcpolis.entrypoints.routes.dashboard_auth import get_signing_key
    signing_key = get_signing_key(settings)
    auth_coordinator = PendingAuthCoordinator(signing_key)

    # OrgService is built early (before the gateway / MCP servers)
    # because the gateway controller needs it to resolve a user's
    # memberships when serving the multi-org cloud /mcp URL. The
    # second wiring step further down (set_org_service on the OAuth
    # provider) reuses the same instance.
    org_service = OrgServiceCls(
        org_repo=storage.organization_repo,
        config_repo=storage.config_repo,
        service_token_repo=storage.service_token_repo,
    )

    # Create low-level MCP server (gateway)
    mcp_server = create_mcp_server(runtime_manager, org_service=org_service)
    session_manager = StreamableHTTPSessionManager(app=mcp_server)
    session_registry = GatewaySessionRegistry()

    # Log client_disconnect when sessions are cleaned up
    def _on_session_disconnect(
        session_id: str, org_id: str, user_id: str
    ) -> None:
        from datetime import UTC, datetime
        from mcpolis.domain.model.audit import AuditEntry

        entry = AuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            action="client_disconnect",
            org_id=org_id,
            user_id=user_id,
            upstream_id="",
            session_id=session_id,
            outcome="success",
        )
        asyncio.get_event_loop().create_task(audit_repo.log(org_id, entry))

    session_registry.set_on_disconnect(_on_session_disconnect)

    policy_notifier = PolicyNotifier(
        session_manager, session_registry, runtime_manager,
        connection_store=connection_store,
        server_url=settings.server_url,
        debounce_seconds=settings.policy_notifier_debounce_seconds,
    )
    # Forward upstream-side {tools,resources,prompts}/list_changed
    # notifications through the same notifier so downstream MCP clients
    # learn about upstream catalogue changes without polling.
    runtime_manager.set_on_upstream_tools_changed(
        policy_notifier.notify_upstream_tools_changed,
    )
    runtime_manager.set_on_upstream_resources_changed(
        policy_notifier.notify_upstream_resources_changed,
    )
    runtime_manager.set_on_upstream_prompts_changed(
        policy_notifier.notify_upstream_prompts_changed,
    )

    # Build the MCP Starlette app. The gateway OAuth surface (bearer
    # tokens for MCP clients) is always Google-backed; in standalone
    # dev mode without Google credentials the gateway is mounted but
    # only ``mint_test_token`` works (test_mode minted bearer tokens).
    # The dashboard browser-login path is decoupled — see
    # ``effective_dashboard_provider`` / ``dashboard_oauth_provider``
    # below.
    service_token_service = ServiceTokenService(storage.service_token_repo)

    mcp_app = _build_mcp_app_with_oauth(
        session_manager, settings, runtime_manager,
        storage.oauth_state_repo,
        org_service=org_service,
        service_token_service=service_token_service,
        event_bus=event_bus, session_registry=session_registry,
        audit_repo=audit_repo,
    )

    gateway_provider = mcp_app.state.mcp_gateway_oauth_provider  # type: ignore[union-attr]
    gateway_connected_users_fn = gateway_provider.get_connected_users
    revoke_gateway_user_fn = gateway_provider.revoke_user_tokens

    # Create admin MCP server
    admin_mcp = create_admin_mcp_server(
        runtime_manager,
        audit_repo,
        policy_store=policy_store,
        connection_store=connection_store,
        auth_coordinator=auth_coordinator,
        server_url=settings.server_url,
        event_bus=event_bus,
        revoke_gateway_user=revoke_gateway_user_fn,
        terminate_gateway_sessions=policy_notifier.terminate_user_sessions,
        allow_stdio_mcp=settings.allow_stdio_mcp,
        org_repo=storage.organization_repo,
        service_token_service=service_token_service,
    )
    admin_mcp_starlette = admin_mcp.streamable_http_app()

    admin_guarded_app: ASGIApp = _build_admin_app_with_oauth(
        admin_mcp_starlette, gateway_provider, settings, runtime_manager,
    )

    # FastAPI lifespan: start session managers + upstream connections
    admin_session_manager = admin_mcp.session_manager
    startup_task: asyncio.Task[None] | None = None

    async def _connect_all_orgs_background() -> None:
        """Initialize and connect runtimes for all orgs."""
        if settings.mode == "standalone":
            # Default org runtime was created synchronously above;
            # just connect its upstreams.
            default_runtime = runtime_manager.get_cached(DEFAULT_ORG_ID)
            if default_runtime is not None:
                await runtime_manager.connect_runtime(default_runtime)
        else:
            # Cloud mode: create runtimes for all existing orgs.
            all_orgs = await storage.organization_repo.list_organizations()
            org_ids = [org.id for org in all_orgs]
            await runtime_manager.connect_all_background(org_ids)
        # After runtimes are connected, seed the bundled demo upstream
        # (idempotent — skips if already registered).
        if settings.demo_seed:
            await _seed_demo_upstream()

    async def _seed_demo_upstream() -> None:
        """Idempotently register the bundled demo as a service-account
        upstream on the right org. Standalone → DEFAULT_ORG_ID; cloud →
        first org if any exist (best-effort dev hook).
        """
        target_org_id: str
        if settings.mode == "standalone":
            target_org_id = DEFAULT_ORG_ID
        else:
            orgs = await storage.organization_repo.list_organizations()
            if not orgs:
                logger.info("demo_upstream.seed.skipped_no_orgs")
                return
            target_org_id = orgs[0].id

        runtime = await runtime_manager.get(target_org_id)
        existing = await runtime.config_service.get_upstream(
            target_org_id, settings.demo_upstream_id,
        )
        if existing is not None:
            logger.info(
                "demo_upstream.seed.already_present",
                org_id=target_org_id,
                upstream_id=settings.demo_upstream_id,
            )
            return

        demo_url = settings.server_url.rstrip("/") + "/dev/mcp-demo/mcp"
        upstream_def = UpstreamDefinition(
            id=settings.demo_upstream_id,
            display_name=DEMO_UPSTREAM_DISPLAY_NAME,
            transport=TransportType.streamable_http,
            http=HttpTransportConfig(url=demo_url),
            auth=UpstreamAuthConfig(mode=AuthMode.service_account),
        )
        await runtime.config_service.add_upstream(
            target_org_id, upstream_def,
        )
        try:
            await runtime.client_manager.connect_shared(upstream_def)
            await runtime.tool_registry.refresh_upstream(
                settings.demo_upstream_id,
            )
        except Exception:  # noqa: BLE001 — log + continue startup
            logger.exception(
                "demo_upstream.seed.connect_failed",
                org_id=target_org_id,
                upstream_id=settings.demo_upstream_id,
            )
        logger.info(
            "demo_upstream.seeded",
            org_id=target_org_id,
            upstream_id=settings.demo_upstream_id,
            url=demo_url,
        )

    # One listener task per org. Each subscribes to its own channel
    # so policy_changed events reach PolicyNotifier with the correct
    # org_id — matches the tenant isolation RedisEventStream enforces
    # at the channel level.
    policy_listener_tasks: dict[str, asyncio.Task[None]] = {}

    async def _listen_for_org(org_id: str) -> None:
        # Subscribe with a sentinel subscriber id rather than the
        # wildcard ``"*"``. RedisEventStream refuses wildcard
        # subscriptions (anti-enumeration) — using it here silently
        # killed the cloud-mode broadcast for months. Policy events
        # all have ``user_email=None`` which both backends deliver to
        # every subscriber regardless of declared user.
        async for event in event_bus.subscribe(org_id, "__policy_listener__"):
            if event is None:
                continue
            if event.type != "policy_changed":
                continue
            # org_id of the event is the one we're subscribed to by
            # construction; the payload can still carry an override
            # (e.g. for cross-org admin actions), but default to the
            # subscription org.
            payload_org = event.payload.get("org_id", org_id)
            if not isinstance(payload_org, str):
                payload_org = org_id
            role = event.payload.get("role")
            user = event.payload.get("user")
            if isinstance(role, str):
                policy_notifier.notify_role_changed(payload_org, role)
            elif isinstance(user, str):
                policy_notifier.notify_user_changed(payload_org, user)
            else:
                policy_notifier.notify_all_roles()

    def _spawn_policy_listener(org_id: str) -> None:
        """Idempotently start a policy-event listener for this org."""
        if org_id in policy_listener_tasks:
            return
        policy_listener_tasks[org_id] = asyncio.create_task(
            _listen_for_org(org_id),
            name=f"policy-listener-{org_id}",
        )

    # Shared §5.2 re-auth notification wiring. Built once so the
    # periodic token-refresh loop (which notifies inline before deleting
    # an invalid_grant token) and the hourly health-email sweep can't
    # drift in how they reach users. A configured ``MCPOLIS_SMTP_HOST``
    # selects the real SMTP transport (Google Workspace submission in
    # prod); empty falls back to the logging ``StubEmailSender`` for
    # dev / standalone. Reuses ``signing_key`` (built above) as the
    # re-auth link HMAC key.
    from mcpolis.domain.ports.email_sender import EmailSender
    health_email_sender: EmailSender
    if settings.smtp_host:
        from mcpolis.adapters.email.smtp_email_sender import SmtpEmailSender
        health_email_sender = SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            from_addr=settings.smtp_from,
            from_name=settings.smtp_from_name,
        )
        logger.info(
            "email.transport.smtp",
            host=settings.smtp_host,
            port=settings.smtp_port,
            from_addr=settings.smtp_from,
        )
    else:
        from mcpolis.adapters.email.stub_email_sender import StubEmailSender
        health_email_sender = StubEmailSender()

    async def _resolve_org_admin_emails(org_id: str) -> list[str]:
        # Source of truth is the org's policy config — the membership
        # row's ``role`` is a denormalization that may lag a policy edit.
        # Routing through ``policy_engine`` also picks up *any* role
        # flagged ``is_admin=True``, not just one literally named "admin".
        runtime = await runtime_manager.get(org_id)
        return runtime.policy_engine.get_admin_emails()

    async def _periodic_token_refresh_all() -> None:
        """Background loop that refreshes OAuth tokens for all orgs."""
        from mcpolis.domain.services.oauth_refresh import (
            TOKEN_REFRESH_INTERVAL,
        )
        while True:
            await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
            # Only thread the §5.2 notifier through when the feature flag
            # is on; otherwise the inline notify is skipped (deps stay
            # None) and the loop just deletes the dead token as before.
            notify_on = settings.upstream_health_email_enabled
            for org_id, runtime in list(runtime_manager.all_runtimes.items()):
                try:
                    from mcpolis.domain.services.oauth_refresh import (
                        refresh_token_for_user,
                    )
                    from mcpolis.domain.model.policy import AuthMode
                    oauth_upstreams = [
                        u for u in runtime.live_upstreams()
                        if u.auth.mode in (AuthMode.admin_oauth, AuthMode.per_user_oauth)
                    ]
                    upstream_by_id = {u.id: u for u in oauth_upstreams}
                    all_tokens = await connection_store.get_all_stored_tokens(org_id)
                    for upstream_id, user_id in all_tokens:
                        upstream = upstream_by_id.get(upstream_id)
                        if upstream is None:
                            continue
                        # Bind per-iteration context so log lines emitted
                        # during this refresh — including the MCP SDK's
                        # ``mcp.client.auth.oauth2`` ERROR records and any
                        # httpx output, which carry no upstream/user of
                        # their own — automatically gain org/upstream/user
                        # via ``foreign_pre_chain``'s ``merge_contextvars``.
                        # Scoped via ``bound_contextvars`` so each iteration's
                        # bindings can't leak into the next.
                        with bound_contextvars(
                            org_id=org_id,
                            upstream_id=upstream.id,
                            user_id=user_id,
                        ):
                            await refresh_token_for_user(
                                org_id, upstream, user_id,
                                connection_store, settings.server_url,
                                distributed_lock=storage.distributed_lock,
                                email_sender=(
                                    health_email_sender if notify_on else None
                                ),
                                admin_email_resolver=(
                                    _resolve_org_admin_emails if notify_on else None
                                ),
                                hmac_key=signing_key if notify_on else None,
                            )
                except Exception:
                    logger.exception(
                        "oauth.token.refresh.periodic.failed",
                        org_id=org_id,
                    )

    async def _periodic_health_email_all() -> None:
        """§5.2 loop: hourly, for each org, check stored tokens and
        send re-auth emails to users whose refresh has been
        recorded as ``invalid_grant``. Uses the ``StubEmailSender``
        today — real adapter swap lands in a follow-up. Stays
        on-by-default because the stub is a no-op outward (just logs
        + in-memory record); the ``upstream_health_email_enabled``
        flag controls whether anything leaves the process once a
        real adapter is wired.
        """
        from mcpolis.domain.services.upstream_health_check import (
            HEALTH_EMAIL_INTERVAL,
            run_health_check_for_org,
        )

        while True:
            await asyncio.sleep(HEALTH_EMAIL_INTERVAL)
            if not settings.upstream_health_email_enabled:
                logger.debug("upstream.health_email.disabled_flag_skipped_tick")
                continue
            for org_id, runtime in list(runtime_manager.all_runtimes.items()):
                try:
                    await run_health_check_for_org(
                        org_id=org_id,
                        upstreams=runtime.live_upstreams(),
                        connection_store=connection_store,
                        email_sender=health_email_sender,
                        admin_email_resolver=_resolve_org_admin_emails,
                        server_url=settings.server_url,
                        hmac_key=signing_key,
                    )
                except Exception:
                    logger.exception(
                        "upstream.health_email.loop.failed",
                        org_id=org_id,
                    )

    async def _periodic_liveness_probe_all() -> None:
        """Background loop that probes live OAuth sessions for all orgs.

        Separate from the token-refresh loop because it works off a
        different clock (hourly vs 10-min) and has different failure
        semantics — the probe tears down sessions, the refresh loop
        doesn't.
        """
        from mcpolis.domain.services.oauth_liveness import (
            HEALTH_CHECK_INTERVAL,
            run_liveness_probes_for_org,
        )
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            for org_id, runtime in list(runtime_manager.all_runtimes.items()):
                try:
                    await run_liveness_probes_for_org(
                        org_id=org_id,
                        upstreams=runtime.live_upstreams(),
                        connection_store=connection_store,
                        client_manager=runtime.client_manager,
                        server_url=settings.server_url,
                    )
                except Exception:
                    logger.exception(
                        "oauth.liveness.periodic.failed",
                        org_id=org_id,
                    )

    @contextlib.asynccontextmanager
    async def app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal startup_task
        # Cloud mode: create Mongo indexes and sync membership rows.
        # Standalone mode: ensure the default org + roles exist.
        await initialize_storage(
            storage, audit_retention_days=settings.audit_retention_days,
        )

        # MCPOLIS_E2B_FRESH_SANDBOXES one-shot override. Operator
        # sets this when they want the next boot to start from a
        # clean slate — kills every persisted sandbox + clears
        # every ref + falls through to fresh creates. Runs BEFORE
        # the reconciler so the reconciler doesn't see anything to
        # reconcile against. After the wipe, operator unsets the
        # flag and subsequent deploys go through the normal
        # reuse-on-restart path.
        if settings.e2b_fresh_sandboxes:
            from mcpolis.adapters.sandbox_e2b import (  # noqa: PLC0415
                E2BSandboxService,
            )
            e2b_svc = sandbox_services.get("e2b")
            if isinstance(e2b_svc, E2BSandboxService):
                cleared = await e2b_svc.wipe_for_fresh_restart()
                logger.info(
                    "sandbox.e2b.fresh_restart.applied",
                    cleared=cleared,
                )

        # Per-backend startup reconciler (plan §"Resilience"). Runs
        # before traffic is accepted so orphan sandboxes from a prior
        # crashed instance get cleaned up before any new sessions
        # could overlap with them. Only fires when:
        #
        # - The active provider is e2b (own-runner reconciler is
        #   deferred — see ``internal/documents/stdio-mcp-sandboxing.md``
        #   §9 OPEN question).
        # - Mongo persistence is wired (an empty in-memory
        #   repository would cause the reconciler to think every
        #   live sandbox is an orphan and kill them all — the plan
        #   explicitly forbids running reconcile against empty
        #   persistence).
        await _run_sandbox_reconcile_at_boot(
            settings=settings,
            storage=storage,
            sandbox_services=sandbox_services,
            mcpolis_instance=mcpolis_instance,
            event_stream=storage.event_stream,
        )

        # Standalone mode: reload policy engine from persisted config
        # (may have changed since the sync load above).
        if settings.mode == "standalone":
            default_runtime = runtime_manager.get_cached(DEFAULT_ORG_ID)
            if default_runtime is not None:
                persisted = await storage.config_repo.load(DEFAULT_ORG_ID)
                default_runtime.policy_engine.reload(persisted)
                logger.info(
                    "policy.engine.loaded",
                    org_id=DEFAULT_ORG_ID,
                    role_count=len(persisted.roles),
                    user_count=len(persisted.users),
                )

        await gateway_provider.load_state()
        # Build a stack of session-manager contexts. The bundled demo
        # adds one more if mounted — its ``session_manager.run()`` MUST
        # run for /dev/mcp-demo/mcp requests to succeed (Starlette Mount
        # does not propagate the inner FastMCP lifespan).
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(session_manager.run())
            await stack.enter_async_context(admin_session_manager.run())
            if settings.demo_mount:
                demo_mcp_obj = getattr(app.state, "demo_mcp", None)
                if demo_mcp_obj is not None:
                    await stack.enter_async_context(
                        demo_mcp_obj.session_manager.run(),
                    )
            startup_task = asyncio.create_task(_connect_all_orgs_background())
            # Spawn one policy-event listener per existing org so events
            # from every tenant reach PolicyNotifier. New orgs created
            # later go through the ``on_org_created`` hook below, which
            # spawns their listener on demand.
            if settings.mode == "cloud":
                existing_orgs = await storage.organization_repo.list_organizations()
                for existing_org in existing_orgs:
                    _spawn_policy_listener(existing_org.id)
                    runtime_manager.register_display_name(
                        existing_org.id, existing_org.display_name,
                    )
                    runtime_manager.register_slug(
                        existing_org.id, existing_org.slug,
                    )
            else:
                _spawn_policy_listener(DEFAULT_ORG_ID)
            token_refresh_task = asyncio.create_task(
                _periodic_token_refresh_all()
            )
            liveness_probe_task = asyncio.create_task(
                _periodic_liveness_probe_all()
            )
            health_email_task = asyncio.create_task(
                _periodic_health_email_all()
            )

            # Register SIGTERM handler for graceful drain.
            import signal
            loop = asyncio.get_running_loop()

            def _on_sigterm() -> None:
                logger.info("app.sigterm.drain_started")
                asyncio.create_task(drain.drain())

            try:
                loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
            except NotImplementedError:
                pass  # Windows doesn't support add_signal_handler

            try:
                yield
            finally:
                # Wait for in-flight requests to finish.
                await drain.drain()
                token_refresh_task.cancel()
                liveness_probe_task.cancel()
                health_email_task.cancel()
                for _task in policy_listener_tasks.values():
                    _task.cancel()
                if startup_task and not startup_task.done():
                    startup_task.cancel()
                # Mark every active sandbox session as
                # preserve-on-close BEFORE tearing down runtimes.
                # Without this, the per-session finally block in the
                # sandbox service would kill the sandbox (the new
                # default — see ``_session_cm``), and the next boot
                # would face a cold-install for every upstream. With
                # this, sandboxes survive the deploy and the next
                # boot's ``_try_reconnect`` reattaches in ~700 ms.
                for _svc in sandbox_services.values():
                    mark = getattr(
                        _svc,
                        "mark_all_active_sessions_preserve_on_close",
                        None,
                    )
                    if mark is None:
                        continue
                    try:
                        marked = mark()
                    except Exception:
                        logger.warning(
                            "sandbox.shutdown.preserve_mark_failed",
                            provider=getattr(_svc, "name", "?"),
                            exc_info=True,
                        )
                        continue
                    logger.info(
                        "sandbox.shutdown.preserve_marked",
                        provider=getattr(_svc, "name", "?"),
                        marked=marked,
                    )
                await runtime_manager.shutdown_all()
                await storage.event_stream.close()
                await storage.rate_limiter.close()
                await storage.distributed_lock.close()
                if storage.mongo is not None:
                    storage.mongo.close()

    app = FastAPI(title="MCP Hero Gateway", version="0.1.0", lifespan=app_lifespan)

    @app.exception_handler(PlanLimitExceeded)
    async def _plan_limit_exceeded_handler(
        request: Request, exc: PlanLimitExceeded,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=402,
            content={
                "error": "plan_limit_exceeded",
                "gate": exc.gate,
                "current": exc.current,
                "limit": exc.limit,
                "message": exc.message,
            },
        )

    # Starlette Mount only routes "/mcp/..." (with trailing slash).  Clients
    # (e.g. Claude Code) send to "/mcp" (no slash).  Instead of a 307 redirect
    # (which can cause clients to drop auth headers), rewrite the path in-place
    # at the ASGI level so the mount sees it as "/mcp/".
    _bare_mount_paths = {b"/mcp", b"/admin-mcp"}

    class _TrailingSlashMiddleware:
        def __init__(self, asgi_app: ASGIApp) -> None:
            self.app = asgi_app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http" and scope.get("raw_path") in _bare_mount_paths:
                scope = dict(scope)
                scope["path"] = scope["path"] + "/"
                scope["raw_path"] = scope["raw_path"] + b"/"
            await self.app(scope, receive, send)

    app.add_middleware(_TrailingSlashMiddleware)  # type: ignore[arg-type]

    # CORS for browser-based MCP clients (MCP Inspector, Claude.ai).
    # Non-browser clients (Claude Code, Claude desktop) ignore these
    # headers. OAuth discovery endpoints are public by design; the
    # MCP endpoints themselves are further gated by bearer auth.
    from starlette.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        # ``mcp-session-id`` is set by the streamable-HTTP transport on
        # the initialize response and must be readable by browser-based
        # clients (MCP Inspector) so they can include it on subsequent
        # POSTs. Without this expose, the browser hides the header and
        # the next POST fails with "Bad Request: Missing session ID".
        expose_headers=["WWW-Authenticate", "mcp-session-id"],
    )

    # Rate limiting removed — the MCP gateway is behind OAuth so
    # anonymous abuse isn't possible. Re-add in Phase 4 when real
    # production traffic data is available to size limits against.

    # Bind per-request context (request_id, path, method) so every log
    # line emitted during the request automatically carries these
    # fields — no plumbing through call chains.
    @app.middleware("http")
    async def log_context_middleware(
        request: Request, call_next: Any,
    ) -> Response:
        clear_contextvars()
        request_id = request.headers.get(
            "x-request-id", uuid.uuid4().hex,
        )
        # ``OrgContextMiddleware`` runs earlier in the ASGI chain and has
        # already populated ``current_org_id``; lift it into structlog
        # context here so EVERY log line emitted during this request —
        # including stdlib loggers (``mcp.client.auth.oauth2``, ``httpx``,
        # ``uvicorn.access``) routed through ``ProcessorFormatter``'s
        # ``foreign_pre_chain`` — carries ``org_id`` without per-call
        # plumbing.
        bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            org_id=current_org_id.get(),
        )
        try:
            return await call_next(request)
        finally:
            clear_contextvars()

    # Middleware to extract user identity. ``current_org_id`` is
    # resolved upstream by ``OrgContextMiddleware`` (installed below),
    # which runs earlier in the ASGI chain so its decision is in
    # effect by the time this middleware fires.
    @app.middleware("http")
    async def drain_middleware(request: Request, call_next: Any) -> Response:
        # Health checks bypass drain so the LB can query status.
        if request.url.path in ("/healthz", "/health"):
            return await call_next(request)
        if drain.is_draining:
            return JSONResponse(
                {"detail": "Server is shutting down"},
                status_code=503,
            )
        drain.request_started()
        try:
            response: Response = await call_next(request)
            return response
        finally:
            drain.request_finished()

    @app.middleware("http")
    async def user_identity_middleware(request: Request, call_next: Any) -> Response:
        # User identity for log/audit/sentry binding. Two paths feed it:
        #
        # * Dashboard cookie auth (``mcpolis_session``): we verify the
        #   signed cookie cheaply (HMAC, no DB call) and lift the email
        #   into ``current_user_id`` and the structlog contextvar. This
        #   is what makes ``user_id:<email>`` filters in Discover work
        #   for ``/api/*`` traffic — without it, every authenticated
        #   request still showed up as ``user_id: anonymous`` in the
        #   uvicorn.access output.
        # * MCP / admin-mcp OAuth bearer paths set ``auth_context_var``
        #   later in the chain; ``_get_current_user`` reads that first,
        #   so this middleware doesn't need to handle them.
        user_id = "anonymous"
        cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie_value:
            payload = get_session_payload(settings, cookie_value)
            if payload is not None:
                email = payload.get("email")
                if isinstance(email, str) and email:
                    user_id = email
        session_id = request.headers.get("mcp-session-id")
        token = current_user_id.set(user_id)
        session_token = current_session_id.set(session_id)
        # Bind org_id alongside user/session so it survives to
        # uvicorn.access (the inner ``log_context_middleware`` calls
        # ``clear_contextvars()`` in its finally, which wipes any bind
        # made inside it — including my earlier attempt to bind org_id
        # there. user/session already proved this outer bind site
        # survives that clear, so org_id rides the same coattails).
        bind_contextvars(
            user_id=user_id,
            session_id=session_id,
            org_id=current_org_id.get(),
        )
        try:
            response: Response = await call_next(request)
            return response
        finally:
            current_user_id.reset(token)
            current_session_id.reset(session_token)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Load-balancer health check — returns 503 when draining."""
        if drain.is_draining:
            return JSONResponse(
                {"status": "draining"},
                status_code=503,
            )
        return JSONResponse({"status": "ok"})

    @app.get("/api/config/features")
    async def features() -> dict[str, object]:
        """Runtime feature flags consumed by the frontend.

        ``mode`` is surfaced here so the SPA can hide the org switcher
        and signup flow when running against a standalone install.
        ``sandbox_provider`` lets stdio-related copy reflect whether
        the active backend is the E2B sandbox or the dev-only
        local-subprocess path.
        """
        requested = settings.sandbox_provider.strip()
        if requested:
            sandbox_provider = requested
        else:
            sandbox_provider = "e2b" if settings.e2b_api_key else "local-subprocess"
        return {
            "allow_stdio_mcp": settings.allow_stdio_mcp,
            "mode": settings.mode,
            "sandbox_provider": sandbox_provider,
        }

    # Root-level OAuth redirects — Claude Code probes multiple URL patterns.
    from starlette.responses import RedirectResponse as StarletteRedirect

    @app.get("/.well-known/oauth-authorization-server")
    async def root_oauth_metadata() -> StarletteRedirect:
        return StarletteRedirect(url="/mcp/.well-known/oauth-authorization-server")

    @app.get("/.well-known/oauth-authorization-server/{path:path}")
    async def root_oauth_metadata_with_path(path: str) -> StarletteRedirect:
        return StarletteRedirect(url="/mcp/.well-known/oauth-authorization-server")

    @app.get("/.well-known/oauth-protected-resource")
    async def root_resource_metadata() -> StarletteRedirect:
        return StarletteRedirect(url="/mcp/.well-known/oauth-protected-resource")

    # RFC 9728: protected-resource discovery can include the
    # resource's URL path as a suffix. Clients following the
    # WWW-Authenticate ``resource_metadata`` hint hit paths like
    # ``/.well-known/oauth-protected-resource/mcp/acme``.
    # Without this handler the SPA fallback catches the request and
    # returns the marketing HTML, breaking discovery.
    @app.get("/.well-known/oauth-protected-resource/{path:path}")
    async def root_resource_metadata_with_path(path: str) -> Any:
        # Parse the resource path (e.g. "mcp/acme" or "admin-mcp/acme")
        # and return metadata with the fully-qualified slug-scoped
        # resource URL. If the path doesn't match one of our known
        # mounts, fall back to the unscoped default. The gateway and
        # admin-mcp may live on different origins (subdomain split),
        # so each prefix gets its own root.
        admin_root = settings.server_url.rstrip("/")
        gateway_root = effective_gateway_url(settings)
        parts = path.strip("/").split("/")
        slug = ""
        prefix = "mcp"
        if len(parts) >= 1 and parts[0] in {"mcp", "admin-mcp"}:
            prefix = parts[0]
            if len(parts) >= 2:
                slug = parts[1]
        root = gateway_root if prefix == "mcp" else admin_root
        base = f"{root}/{prefix}"
        resource = f"{base}/{slug}" if slug else base
        return JSONResponse({
            "resource": resource,
            "authorization_servers": [f"{root}/{prefix}"],
        }, headers={"Cache-Control": "public, max-age=3600"})

    @app.api_route("/register", methods=["POST"])
    async def root_register(request: Request) -> StarletteRedirect:
        return StarletteRedirect(url="/mcp/register", status_code=307)

    # Admin MCP OAuth discovery redirects
    @app.get("/.well-known/oauth-protected-resource/admin-mcp")
    async def admin_resource_metadata() -> StarletteRedirect:
        return StarletteRedirect(
            url="/admin-mcp/.well-known/oauth-protected-resource"
        )

    @app.get("/.well-known/oauth-authorization-server/admin-mcp")
    async def admin_oauth_metadata() -> StarletteRedirect:
        return StarletteRedirect(
            url="/admin-mcp/.well-known/oauth-authorization-server"
        )

    # Store auth coordinator, connection store, and event bus on app state for the callback route
    app.state.auth_coordinator = auth_coordinator  # type: ignore[attr-defined]
    app.state.connection_store = connection_store  # type: ignore[attr-defined]
    app.state.event_bus = event_bus  # type: ignore[attr-defined]
    app.state.runtime_manager = runtime_manager  # type: ignore[attr-defined]

    # Surface the gateway OAuth provider on the outer app's state so
    # tests / integration callers can mint bearer tokens via
    # ``mint_test_token`` without reaching into the mounted Starlette
    # sub-app's state.
    app.state.mcp_gateway_oauth_provider = gateway_provider  # type: ignore[attr-defined]

    # Upstream OAuth callback route
    from mcpolis.entrypoints.routes.upstream_oauth_callback import (
        create_upstream_oauth_callback_route,
    )
    upstream_callback = create_upstream_oauth_callback_route()
    app.add_api_route(
        upstream_callback.path,
        upstream_callback.endpoint,  # type: ignore[arg-type]
        methods=["GET"],
    )

    # Dashboard auth + API
    from mcpolis.entrypoints.middleware.org_context import (
        OrgContextMiddleware,
        SlugCache,
    )
    from mcpolis.entrypoints.routes.org_routes import create_org_router

    # ``org_service`` was built earlier (see Phase 0 wiring above)
    # so the gateway controller can use it; reuse the same instance
    # here for the dashboard, OAuth provider, and middleware.

    # Wire the OrgService into the gateway OAuth provider so the
    # Google callback's policy check ("is this user allowed?") becomes
    # "is the user a member of any org?" — required for the multi-org
    # cloud /mcp flow.
    gateway_provider.set_org_service(org_service)

    dashboard_oauth_provider: DashboardOAuthProvider
    effective_provider = effective_dashboard_provider(settings)
    if effective_provider == "google":
        from mcpolis.adapters.auth.google_oauth_provider import (
            DashboardGoogleProvider,
        )
        dashboard_oauth_provider = DashboardGoogleProvider(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
    else:
        from mcpolis.adapters.auth.dev_stub_oauth_provider import (
            DevStubOAuthProvider,
        )
        # ``validate_startup_secrets`` already accepted this combination
        # (cloud + test_mode + loopback bind is the only way to reach
        # this branch in cloud mode). Forward the same allowance to
        # the provider's defense-in-depth check.
        dashboard_oauth_provider = DevStubOAuthProvider(
            mode=settings.mode,
            allow_in_cloud=settings.mode == "cloud",
        )
    logger.info("dashboard.oauth.provider", name=effective_provider)

    dashboard_auth = create_dashboard_auth(
        settings, runtime_manager, policy_store, org_service,
        dashboard_oauth_provider,
        gateway_oauth_provider=gateway_provider,
        session_revocation=storage.session_revocation,
    )
    app.include_router(dashboard_auth.router)
    # Shared slug cache: the org-context middleware fills it, the org
    # routes invalidate it after each ``create_org``. Sharing a tiny
    # mutable object sidesteps the fact that FastAPI instantiates
    # middleware lazily.
    slug_cache = SlugCache()

    def _on_org_created(new_org: Any) -> None:
        # Invalidate the slug→org_id cache so the new slug is
        # immediately resolvable, then spawn a policy-event listener
        # for the new tenant (no-op for the delete path because the
        # task guard is idempotent — deleted orgs keep their listener
        # until shutdown, harmlessly). Also register the display name
        # so MCP ``initialize`` instructions can be org-scoped without
        # an async lookup.
        slug_cache.invalidate()
        _spawn_policy_listener(new_org.id)
        runtime_manager.register_display_name(new_org.id, new_org.display_name)
        runtime_manager.register_slug(new_org.id, new_org.slug)

    app.include_router(
        create_org_router(
            settings,
            org_service,
            dashboard_auth.get_current_user,
            upstream_config_store=storage.upstream_config_repo,
            on_org_created=_on_org_created,
        ),
    )
    # Add the org-context middleware AFTER rate limiting so it runs
    # first in the ASGI stack (FastAPI LIFO). One code path for both
    # modes — in standalone the middleware injects ``/default`` into
    # slug-less ``/mcp`` paths so the same slug-aware pipeline handles
    # every request.
    app.add_middleware(
        OrgContextMiddleware,
        settings=settings,
        org_service=org_service,
        slug_cache=slug_cache,
    )
    app.state.org_service = org_service  # type: ignore[attr-defined]

    def _get_startup_status() -> StartupStatusResponse:
        # Report status for the current org's runtime only.
        org_id = current_org_id.get()
        status = runtime_manager.get_startup_status(org_id)
        return StartupStatusResponse(
            ready=status.ready,
            total=status.total,
            connected=len(status.connected),
            failed=len(status.failed),
        )

    dashboard_api_router = create_dashboard_api_router(
        runtime_manager=runtime_manager,
        policy_store=policy_store,
        audit_repo=audit_repo,
        connection_store=connection_store,
        auth_coordinator=auth_coordinator,
        server_url=settings.server_url,
        gateway_url=effective_gateway_url(settings),
        get_current_user=dashboard_auth.get_current_user,
        require_admin=dashboard_auth.require_admin,
        get_startup_status=_get_startup_status,
        get_gateway_connected_users=gateway_connected_users_fn,
        revoke_gateway_user=revoke_gateway_user_fn,
        terminate_gateway_sessions=policy_notifier.terminate_user_sessions,
        event_bus=event_bus,
        list_admin_mcp_tools=admin_mcp.list_tools,
        allow_stdio_mcp=settings.allow_stdio_mcp,
        org_repo=storage.organization_repo,
        is_cloud_mode=settings.mode == "cloud",
        template_var_repo=storage.template_var_repo,
        sandbox_file_repo=storage.sandbox_file_repo,
        service_token_service=service_token_service,
    )
    app.include_router(dashboard_api_router)

    # Sandbox admin routes (resource picker + capabilities). The
    # Phase H lifecycle / sessions / kill-switch / sandbox-usage
    # routes were dropped along with the corresponding admin-UI
    # surfaces; only the resource-picker round-trip survives.
    from mcpolis.entrypoints.routes.sandbox_routes import (
        create_sandbox_router,
    )
    app.include_router(
        create_sandbox_router(
            require_admin=dashboard_auth.require_admin,
            runtime_manager=runtime_manager,
            audit_repo=audit_repo,
            event_bus=event_bus,
            org_repo=storage.organization_repo,
        ),
    )

    # Debug routes for observability smoke-tests. Always registered,
    # but each route returns 404 unless the caller's email is in
    # MCPOLIS_SUPERADMIN_EMAILS. Backs the /superadmin/test-observability
    # frontend page; non-superadmins see 404 so the routes look invisible.
    from mcpolis.entrypoints.routes.debug_routes import create_debug_router
    debug_superadmin_emails = settings.parsed_superadmin_emails()
    app.include_router(
        create_debug_router(
            analytics,
            health_email_sender,
            dashboard_auth.get_current_user,
            debug_superadmin_emails,
        )
    )
    logger.info(
        "debug_routes.enabled",
        prefix="/api/debug",
        superadmins=len(debug_superadmin_emails),
    )

    # Client-side error / event ingest. Open by design — errors that
    # happen pre-login (signup-page silent aborts, OAuth-callback
    # crashes) are exactly the records we most need. Bounded by a
    # 4 KB body cap and the Vector throttle.
    from mcpolis.entrypoints.routes.client_errors import (
        create_client_errors_router,
    )

    def _optional_user(request: Request) -> str | None:
        cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
        payload = get_session_payload(settings, cookie_value)
        if payload is None:
            return None
        email = payload.get("email")
        return email if isinstance(email, str) else None

    app.include_router(create_client_errors_router(_optional_user))

    # Operator routes (egress denylist) and the EgressDenylistRepository
    # were deleted along with the runner-side Envoy enforcer; the
    # current sandbox provider (E2B) doesn't expose a comparable
    # network-policy primitive. See plan ``serene-beaming-tulip.md``
    # §Phase 4.

    # Superadmin dashboard routes (cross-org browse + soft actions).
    # Reuses the same email allowlist as debug/operator/system MCP,
    # but returns 403 (not 404) because the dashboard is a deliberate,
    # named feature surfaced via a sidebar link.
    from mcpolis.entrypoints.routes.superadmin_routes import (
        create_superadmin_router,
    )
    app.include_router(
        create_superadmin_router(
            settings=settings,
            org_repo=storage.organization_repo,
            runtime_manager=runtime_manager,
            org_service=org_service,
            audit_repo=audit_repo,
            connection_store=connection_store,
            revoke_gateway_user=revoke_gateway_user_fn,
            terminate_gateway_sessions=policy_notifier.terminate_user_sessions,
            get_current_user=dashboard_auth.get_current_user,
            superadmin_emails=debug_superadmin_emails,
        ),
    )

    # Mount MCP endpoints
    app.mount("/mcp", mcp_app)

    # Superadmin MCP: cloud mode only, gated by email allowlist.
    # Mounted before /admin-mcp so /admin-mcp/system is caught first.
    if settings.mode == "cloud":
        superadmin_emails_set = settings.parsed_superadmin_emails()
        if superadmin_emails_set:
            from mcpolis.entrypoints.controllers.superadmin_controller import (
                create_superadmin_mcp_server,
            )

            superadmin_mcp = create_superadmin_mcp_server(
                org_repo=storage.organization_repo,
                runtime_manager=runtime_manager,
            )
            superadmin_starlette = superadmin_mcp.streamable_http_app()

            superadmin_provider = mcp_app.state.mcp_gateway_oauth_provider  # type: ignore[union-attr]
            superadmin_guarded = _build_superadmin_app_with_oauth(
                superadmin_starlette,
                superadmin_provider,
                settings,
                superadmin_emails_set,
            )
            app.mount("/admin-mcp/system", superadmin_guarded)  # type: ignore[arg-type]
            logger.info(
                "superadmin.mcp.enabled",
                allowed_email_count=len(superadmin_emails_set),
                mount_path="/admin-mcp/system",
            )

    app.mount("/admin-mcp", admin_guarded_app)  # type: ignore[arg-type]

    # Bundled "kitchen sink" demo upstream. Off by default; flip on
    # via ``MCPOLIS_DEMO_MOUNT=1`` (or ``start.sh --with-demo``).
    # Mount happens here so the path ``/dev/mcp-demo`` is wired before
    # the SPA fallback (registered below) catches everything else.
    # Auto-registration on an org is gated by the separate
    # ``MCPOLIS_DEMO_SEED`` flag and runs from inside the lifespan via
    # ``_seed_demo_upstream`` defined above. ``app.state.demo_mcp``
    # carries the FastMCP instance so the lifespan can enter its
    # ``session_manager.run()`` context; without that, mounted MCP
    # requests fail with "Task group is not initialized."
    if settings.demo_mount:
        demo_app, demo_mcp = build_demo_app(public_url=settings.server_url)
        app.mount("/dev/mcp-demo", demo_app)  # type: ignore[arg-type]
        app.state.demo_mcp = demo_mcp  # type: ignore[attr-defined]
        logger.info(
            "demo_upstream.mounted",
            mount_path="/dev/mcp-demo",
            public_url=settings.server_url,
        )

    # Test-fixture stdio MCP. Off by default; flip via
    # ``MCPOLIS_STDIO_TEST_MCP=1`` or ``bash start.sh --with-stdio-test``.
    # Serves the script at ``GET /dev/stdio-test-mcp.py`` so the
    # integration / E2E suites can ``uv run <https-url>`` it inside
    # an E2B sandbox. The fixture itself is a stdio MCP — the route
    # is just a download endpoint. No auth (same threat model as
    # ``/dev/mcp-demo``); don't enable in untrusted environments.
    if settings.stdio_test_mcp:
        from pathlib import Path as _Path  # noqa: PLC0415
        from starlette.responses import PlainTextResponse  # noqa: PLC0415

        _stdio_test_mcp_path = (
            _Path(__file__).parent.parent / "dev" / "stdio_test_mcp_server.py"
        )

        @app.get("/dev/stdio-test-mcp.py")
        async def _serve_stdio_test_mcp() -> PlainTextResponse:
            return PlainTextResponse(
                _stdio_test_mcp_path.read_text(encoding="utf-8"),
                media_type="text/x-python",
            )

        logger.info(
            "stdio_test_mcp.mounted",
            mount_path="/dev/stdio-test-mcp.py",
        )

    # Serve frontend build (production only — set MCPOLIS_FRONTEND_DIST to enable).
    # In dev, the Vite dev server serves the frontend; the backend should not
    # fall back to a stale frontend/dist build.
    frontend_dist = settings.frontend_dist
    if frontend_dist is not None and frontend_dist.exists():
        from starlette.responses import FileResponse, Response
        from starlette.staticfiles import StaticFiles

        # Serve static assets (JS, CSS, fonts) from /assets/
        app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")))

        # SPA fallback: serve index.html for any unmatched route.
        # index.html must never be cached — it references hashed asset filenames
        # that change every build. Hashed assets under /assets/ can be cached
        # forever (handled by StaticFiles defaults).
        no_cache_headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}

        @app.get("/{path:path}")
        async def spa_fallback(path: str) -> Response:
            # Serve actual files if they exist (favicon, etc.)
            file_path = frontend_dist / path
            if file_path.is_file():
                return FileResponse(str(file_path))
            # Refuse to SPA-fallback on paths that clearly aren't app
            # routes: backend prefixes should 404 honestly when a
            # request didn't match a real API route, and scanner-bait
            # extensions (``.php``, ``.asp``, etc.) shouldn't receive
            # a 200 HTML body that makes us look like a live target.
            if _should_reject_spa_fallback(path):
                return Response(status_code=404)
            # Prefer a per-route prerendered HTML file
            # (``dist/<path>/index.html``) over the raw SPA shell. The
            # frontend prerender plugin emits these for marketing routes
            # so non-JS crawlers and social scrapers see real content.
            if path:
                prerendered = frontend_dist / path / "index.html"
                if prerendered.is_file():
                    return FileResponse(
                        str(prerendered),
                        headers=no_cache_headers,
                    )
            return FileResponse(
                str(frontend_dist / "index.html"),
                headers=no_cache_headers,
            )

    return app


# Paths the SPA fallback must not paper over. Specific backend routes
# register before the catch-all (OAuth ``.well-known/*`` endpoints,
# ``/api/*``, ``/mcp/*``, ``/admin-mcp/*``) and win by FastAPI's
# registration order — so reaching this list means the backend has no
# route for the request and the honest answer is 404, not "here's the
# React app." ``.well-known/`` is blanket-rejected for the same reason:
# our OAuth-discovery entries already match ahead of the catch-all, and
# if they didn't we're better off 404-ing than impersonating whatever
# file the scanner was after (ACME challenges, pki-validation probes,
# admin.php etc.). ACME in this deployment is handled at the
# nginx/cert layer, not in the backend process.
_SPA_FALLBACK_REJECT_PREFIXES: tuple[str, ...] = (
    "api/",
    "mcp/",
    "admin-mcp/",
    ".well-known/",
)

# Server-side-script extensions vulnerability scanners probe for. Real
# SPA routes are slash-separated words; they never end in ``.php``.
# Keeping the list tight (only scripts actually associated with a
# hosting platform) so we don't accidentally 404 future frontend files
# that happen to have an extension.
_SPA_FALLBACK_REJECT_EXTENSIONS: tuple[str, ...] = (
    ".php",
    ".asp",
    ".aspx",
    ".jsp",
    ".cgi",
    ".env",
    ".sql",
)


def _should_reject_spa_fallback(path: str) -> bool:
    """True if the SPA catch-all should 404 instead of serving the shell.

    ``path`` is the URL path *without* the leading slash — Starlette
    already strips it when it binds ``{path:path}``. Matching is case-
    insensitive: scanners probe mixed-case variants, and app routes are
    lowercase by convention.
    """
    lower = path.lower()
    if any(lower.startswith(p) for p in _SPA_FALLBACK_REJECT_PREFIXES):
        return True
    return any(lower.endswith(e) for e in _SPA_FALLBACK_REJECT_EXTENSIONS)


def run() -> None:
    settings = Settings()
    # ``log_config=None`` passed to ``uvicorn.run`` disables uvicorn's
    # default dictConfig so every record — ours and uvicorn's — flows
    # through structlog's JSON pipeline.
    configure_structlog(
        json_logs=settings.sentry_environment != "development",
    )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
    )
