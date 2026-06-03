"""Per-org runtime: bundles PolicyEngine, ToolRegistry, UpstreamClientManager,
ToolRouter, and UpstreamConfigService for each organization.

In standalone mode there is exactly one runtime (DEFAULT_ORG_ID).
In cloud mode, runtimes are created lazily on first access or eagerly
at startup for orgs that already have upstream configs.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from collections.abc import Callable

import structlog

from mcpolis.adapters.repositories.audit_repository import (
    AuditRepository as LegacyAuditRepository,
)
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)

OnUpstreamToolsChanged = Callable[[str, str], None]
OnUpstreamResourcesChanged = Callable[[str, str], None]
OnUpstreamPromptsChanged = Callable[[str, str], None]
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import TransportType, UpstreamDefinition
from mcpolis.domain.ports import ConfigRepository, ToolCatalogRepository
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.sandbox_service import (
    SandboxProviderName,
    SandboxService,
)
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.services.upstream_config_service import UpstreamConfigService
from mcpolis.domain.services.upstream_connection_service import (
    reconnect_all_oauth_upstreams,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class OrgRuntime:
    """Per-org bundle of runtime services."""

    org_id: str
    policy_engine: PolicyEngine
    tool_registry: ToolRegistry
    client_manager: UpstreamClientManager
    tool_router: ToolRouter
    config_service: UpstreamConfigService
    # Boot snapshot of upstream definitions, frozen at construction.
    # Drives the boot connect fan-out only. Do NOT read this at request
    # time — it ignores every add/remove/update since boot. Use
    # ``live_upstreams()`` instead.
    upstreams: list[UpstreamDefinition]

    def live_upstreams(self) -> list[UpstreamDefinition]:
        """Upstream definitions as they are *now* — reflecting every
        add / remove / update since construction.

        Reads the tool registry's live registration set, which
        ``UpstreamConfigService`` keeps in sync on every mutation. The
        ``upstreams`` field is a boot snapshot frozen at construction and
        goes stale the moment an upstream is added or removed, so
        request-time consumers MUST use this method. (Reading
        ``upstreams`` directly is what kept a deleted upstream showing on
        the super-admin org page while its detail view 404'd.)
        """
        registry = self.tool_registry
        return [
            defn
            for uid in registry.get_upstream_ids()
            if (defn := registry.get_upstream_definition(uid)) is not None
        ]


@dataclass
class StartupStatus:
    """Tracks per-org startup progress."""

    ready: bool = False
    total: int = 0
    connected: set[str] = field(default_factory=set[str])
    failed: set[str] = field(default_factory=set[str])


class OrgRuntimeManager:
    """Creates, caches, and manages per-org runtime instances."""

    def __init__(
        self,
        config_repo: ConfigRepository,
        upstream_config_repo: UpstreamConfigStore,
        connection_repo: ConnectionStore,
        audit_repo: LegacyAuditRepository,
        tool_catalog_repo: ToolCatalogRepository,
        server_url: str,
        sandbox_resolver: SandboxResolver | None = None,
        sandbox_services: dict[SandboxProviderName, SandboxService] | None = None,
        sandbox_persistence: SandboxPersistenceRepository | None = None,
        mcpolis_instance: str | None = None,
        template_var_repo: TemplateVarRepository | None = None,
        sandbox_file_repo: SandboxFileRepository | None = None,
    ) -> None:
        self._config_repo = config_repo
        self._upstream_config_repo = upstream_config_repo
        self._connection_repo = connection_repo
        self._audit_repo = audit_repo
        self._tool_catalog_repo = tool_catalog_repo
        self._server_url = server_url
        # Sandbox boundary plumbing — propagated to every per-org
        # ``UpstreamClientManager`` so all backends, the resolver,
        # the persistence write-through, and the multi-instance
        # tag are wired uniformly.
        self._sandbox_resolver = sandbox_resolver
        self._sandbox_services = sandbox_services
        self._sandbox_persistence = sandbox_persistence
        self._mcpolis_instance = mcpolis_instance
        # Per-MCP secret store. Resolves ``${NAME}`` refs in stdio env
        # / HTTP headers right before each connection task starts.
        self._template_var_repo = template_var_repo
        # Per-MCP Sandbox files. Threaded through to every per-org
        # ``UpstreamClientManager`` so the launcher renders each
        # file's ``target_path`` (against system + user vars) and
        # writes the file contents into the sandbox before exec.
        self._sandbox_file_repo = sandbox_file_repo

        self._runtimes: dict[str, OrgRuntime] = {}
        self._lock: asyncio.Lock | None = None  # created lazily in async context
        self._startup_status: dict[str, StartupStatus] = {}
        self._on_upstream_tools_changed: OnUpstreamToolsChanged | None = None
        self._on_upstream_resources_changed: OnUpstreamResourcesChanged | None = None
        self._on_upstream_prompts_changed: OnUpstreamPromptsChanged | None = None
        # Sync-accessible cache of org_id → display_name so request-time
        # consumers (e.g. MCP ``initialize`` instructions) can build
        # org-scoped text without an async repository lookup. Populated
        # from ``app.py`` at startup and on org creation.
        self._display_names: dict[str, str] = {}
        # Same shape as ``_display_names`` for the org slug. The
        # resource-wrapping URI scheme bakes the slug in, so the gateway
        # controller needs a sync lookup at list time.
        self._slugs: dict[str, str] = {}

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_cached(self, org_id: str) -> OrgRuntime | None:
        """Sync lookup — returns None if the runtime is not yet initialized."""
        return self._runtimes.get(org_id)

    def set_on_upstream_tools_changed(
        self, callback: OnUpstreamToolsChanged | None,
    ) -> None:
        """Register the (org_id, upstream_id) callback for upstream
        ``tools/list_changed`` notifications. Propagates to every
        existing and future runtime's client_manager.
        """
        self._on_upstream_tools_changed = callback
        for runtime in self._runtimes.values():
            runtime.client_manager.set_on_upstream_tools_changed(
                self._wrap_for_org(runtime.org_id),
            )

    def set_on_upstream_resources_changed(
        self, callback: OnUpstreamResourcesChanged | None,
    ) -> None:
        """Counterpart of ``set_on_upstream_tools_changed`` for
        ``notifications/resources/list_changed``."""
        self._on_upstream_resources_changed = callback
        for runtime in self._runtimes.values():
            runtime.client_manager.set_on_upstream_resources_changed(
                self._wrap_resources_for_org(runtime.org_id),
            )

    def set_on_upstream_prompts_changed(
        self, callback: OnUpstreamPromptsChanged | None,
    ) -> None:
        self._on_upstream_prompts_changed = callback
        for runtime in self._runtimes.values():
            runtime.client_manager.set_on_upstream_prompts_changed(
                self._wrap_prompts_for_org(runtime.org_id),
            )

    def _wrap_for_org(
        self, org_id: str,
    ) -> Callable[[str], None] | None:
        cb = self._on_upstream_tools_changed
        if cb is None:
            return None
        return lambda upstream_id: cb(org_id, upstream_id)

    def _wrap_resources_for_org(
        self, org_id: str,
    ) -> Callable[[str], None] | None:
        cb = self._on_upstream_resources_changed
        if cb is None:
            return None
        return lambda upstream_id: cb(org_id, upstream_id)

    def _wrap_prompts_for_org(
        self, org_id: str,
    ) -> Callable[[str], None] | None:
        cb = self._on_upstream_prompts_changed
        if cb is None:
            return None
        return lambda upstream_id: cb(org_id, upstream_id)

    async def get(self, org_id: str) -> OrgRuntime:
        """Get or lazily create the runtime for an org.

        The hot path (runtime exists) is lock-free. The lock is only
        acquired on first access for a given org_id, and uses a
        double-check pattern to avoid duplicate creation.
        """
        runtime = self._runtimes.get(org_id)
        if runtime is not None:
            return runtime
        async with self._get_lock():
            # Double-check after acquiring lock
            runtime = self._runtimes.get(org_id)
            if runtime is not None:
                return runtime
            return await self._create_runtime_async(org_id)

    def create_runtime_sync(
        self,
        org_id: str,
        config: SettingsConfig,
        upstreams: list[UpstreamDefinition],
    ) -> OrgRuntime:
        """Synchronously create a runtime from pre-loaded parts.

        Used during ``create_app`` (before the event loop) for
        standalone mode where config and upstreams are already loaded
        from files.
        """
        return self._build_runtime(org_id, config, upstreams)

    async def teardown(self, org_id: str) -> None:
        """Stop and remove a runtime (e.g. org deletion)."""
        runtime = self._runtimes.pop(org_id, None)
        if runtime is not None:
            await runtime.client_manager.stop_all()
            self._startup_status.pop(org_id, None)
            logger.info(
                "org.runtime.torn_down",
                org_id=org_id,
            )

    async def connect_runtime(self, runtime: OrgRuntime) -> None:
        """Connect upstreams and discover tools for a runtime.

        Extracted from the old ``_connect_upstreams_background()`` in
        app.py. Public so the lifespan can call it.
        """
        org_id = runtime.org_id
        status = self._startup_status.get(org_id, StartupStatus())
        status.ready = False

        try:
            # Load any persisted tool catalog so the admin permissions
            # UI is populated even if no upstream reconnects this boot.
            await runtime.tool_registry.hydrate()
            # Get disabled upstream IDs
            disabled_ids = await self._connection_repo.get_disabled_ids(org_id)

            # Phase 1: connect service_account upstreams, try discovery for OAuth.
            # Parallelised because each ``connect_shared`` for a stdio
            # service_account upstream blocks on its own E2B sandbox
            # cold-create + npm/uvx package install (~10–25 s each
            # against a warm template). Sequencing N of these turned
            # the deploy "Healthy" gate into N×~25 s. ``connect_shared``
            # state is keyed per ``upstream.id`` so concurrent calls
            # for different upstreams don't contend on the manager.
            # ``return_exceptions=True`` is belt-and-suspenders — the
            # helper already catches per-upstream errors and records
            # them in ``status.failed``, but we don't want one
            # surprise raise to cancel its siblings mid-flight.
            async def _connect_one_phase1(
                upstream: UpstreamDefinition,
            ) -> None:
                if upstream.id in disabled_ids:
                    # Mirror persistence's ``enabled:False`` into the
                    # in-memory state machine so the dashboard renders
                    # DISABLED (rather than the constructor-default
                    # FAILED). Operators answer "why is X stopped
                    # after this deploy?" via the structured
                    # transition log + this skip event.
                    await runtime.client_manager.transition_to_disabled(
                        upstream.id, reason="boot_skip_disabled",
                    )
                    logger.info(
                        "upstream.connect.skipped.disabled",
                        upstream_id=upstream.id,
                        org_id=org_id,
                    )
                    return
                if upstream.auth.mode == AuthMode.service_account:
                    # ``was_ready`` rule for stdio: skip eager connect
                    # entirely unless we have proof the MCP started
                    # successfully before this restart. The cache in
                    # the sandbox persistence ref is that proof — it
                    # only populates after a successful
                    # ``connect_shared``. No cache → never started →
                    # don't waste a sandbox spawn / resume on it.
                    # The earlier auto-disable-after-failure landing
                    # was a band-aid: it stopped the retry on the
                    # *next* boot but still wasted compute on the
                    # current one. Gate up front instead.
                    if upstream.transport == TransportType.stdio:
                        cached = (
                            await runtime.client_manager.try_defer_boot_attach(
                                upstream,
                            )
                        )
                        if cached:
                            status.connected.add(upstream.id)
                            logger.info(
                                "upstream.connect.deferred",
                                upstream_id=upstream.id,
                                org_id=org_id,
                            )
                        else:
                            # No cache → never_ready. The constructor
                            # initialised state to FAILED with
                            # last_failure=None, which is the right
                            # phase. Re-emit the transition log so
                            # operators can grep for it on every boot.
                            await runtime.client_manager.transition_to_failed(
                                upstream.id,
                                last_failure=None,
                                reason="boot_skip_never_ready",
                            )
                            logger.info(
                                "upstream.connect.skipped.never_ready",
                                upstream_id=upstream.id,
                                org_id=org_id,
                            )
                        return

                    # Service-account HTTP: no cache mechanism, but
                    # the cost-per-failed-attempt is also negligible
                    # (TCP handshake + MCP initialize, no sandbox
                    # spawn). Eager connect every boot, with the
                    # auto-disable-on-failure as a circuit breaker
                    # so a permanently broken HTTP MCP eventually
                    # stops being retried.
                    try:
                        await runtime.client_manager.connect_shared(upstream)
                    except Exception as exc:
                        status.failed.add(upstream.id)
                        logger.exception(
                            "upstream.connect.failed",
                            upstream_id=upstream.id,
                            org_id=org_id,
                        )
                        # In-memory: mark DISABLED with last_failure
                        # so the dashboard surfaces why. Persistence:
                        # write ``enabled:False`` so the next boot
                        # respects the auto-disable.
                        await runtime.client_manager.transition_to_disabled(
                            upstream.id,
                            last_failure=str(exc),
                            reason="auto_disable_on_failure",
                        )
                        try:
                            await self._connection_repo.set_disabled(
                                org_id, upstream.id,
                            )
                        except Exception:
                            logger.exception(
                                "upstream.connect.failed.set_disabled_failed",
                                upstream_id=upstream.id,
                                org_id=org_id,
                            )
                        else:
                            logger.info(
                                "upstream.connect.auto_disabled",
                                upstream_id=upstream.id,
                                org_id=org_id,
                                error_class=type(exc).__name__,
                                error=str(exc),
                            )
                        return
                    status.connected.add(upstream.id)
                    logger.info(
                        "upstream.connect.success",
                        upstream_id=upstream.id,
                        org_id=org_id,
                    )
                else:
                    try:
                        await runtime.client_manager.try_discovery_connect(
                            upstream,
                        )
                        status.connected.add(upstream.id)
                    except Exception as exc:
                        status.failed.add(upstream.id)
                        await runtime.client_manager.transition_to_failed(
                            upstream.id,
                            last_failure=str(exc),
                            reason="discovery_failed",
                        )
                        logger.exception(
                            "upstream.discovery.failed",
                            upstream_id=upstream.id,
                            org_id=org_id,
                        )

            await asyncio.gather(
                *(_connect_one_phase1(u) for u in runtime.upstreams),
                return_exceptions=True,
            )

            await runtime.tool_registry.refresh_all()
            runtime.client_manager.start_idle_sweep()

            # Phase 2: reconnect OAuth upstreams with stored tokens
            enabled_upstreams = [
                u for u in runtime.upstreams if u.id not in disabled_ids
            ]
            reasons = await reconnect_all_oauth_upstreams(
                org_id,
                enabled_upstreams,
                self._connection_repo,
                runtime.client_manager,
                runtime.tool_registry,
                self._server_url,
                admin_emails=runtime.policy_engine.get_admin_emails(),
            )
            if reasons:
                logger.info(
                    "upstream.reconnect.results",
                    org_id=org_id,
                    reasons={k: v.value for k, v in reasons.items()},
                )
            await runtime.tool_registry.refresh_all()

            logger.info(
                "org.runtime.ready",
                org_id=org_id,
            )
        except Exception:
            logger.exception(
                "org.runtime.startup.failed",
                org_id=org_id,
            )
        finally:
            status.ready = True

    async def connect_all_background(
        self,
        org_ids: list[str] | None = None,
    ) -> None:
        """Background task: connect runtimes for the given orgs.

        If ``org_ids`` is None, connects all cached runtimes.
        For orgs without a cached runtime, creates one first.
        """
        if org_ids is None:
            org_ids = list(self._runtimes.keys())
        for org_id in org_ids:
            try:
                runtime = self._runtimes.get(org_id)
                if runtime is None:
                    async with self._get_lock():
                        runtime = self._runtimes.get(org_id)
                        if runtime is None:
                            runtime = await self._create_runtime_async(org_id)
                await self.connect_runtime(runtime)
            except Exception:
                logger.exception(
                    "org.runtime.initialization.failed",
                    org_id=org_id,
                )

    async def shutdown_all(self) -> None:
        """Lifespan teardown: stop all runtimes."""
        for org_id in list(self._runtimes):
            await self.teardown(org_id)

    def get_startup_status(self, org_id: str) -> StartupStatus:
        """Return startup status for an org (for the /api/startup endpoint)."""
        return self._startup_status.get(org_id, StartupStatus())

    def register_display_name(self, org_id: str, display_name: str) -> None:
        """Remember an org's display name for sync lookups."""
        self._display_names[org_id] = display_name

    def get_display_name(self, org_id: str) -> str | None:
        """Return the cached display name, or None if unknown."""
        return self._display_names.get(org_id)

    def register_slug(self, org_id: str, slug: str) -> None:
        """Remember an org's slug for sync lookups."""
        self._slugs[org_id] = slug

    def get_slug(self, org_id: str) -> str | None:
        """Return the cached slug, or None if unknown."""
        return self._slugs.get(org_id)

    @property
    def all_runtimes(self) -> dict[str, OrgRuntime]:
        """Read-only view of active runtimes (for periodic tasks)."""
        return self._runtimes

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_runtime(
        self,
        org_id: str,
        config: SettingsConfig,
        upstreams: list[UpstreamDefinition],
    ) -> OrgRuntime:
        """Build a runtime from pre-loaded config and upstreams.

        Sync — does not touch the database. Used by both the sync
        and async creation paths.
        """
        client_manager = UpstreamClientManager(
            upstreams,
            org_id=org_id,
            sandbox_resolver=self._sandbox_resolver,
            sandbox_services=self._sandbox_services,
            sandbox_persistence=self._sandbox_persistence,
            mcpolis_instance=self._mcpolis_instance,
            template_var_repo=self._template_var_repo,
            sandbox_file_repo=self._sandbox_file_repo,
            # Connection repo doubles as a ConnectionStore (see
            # ``MongoConnectionRepository`` class docstring). Threaded
            # through so the dirty-banner snapshot
            # (``started_config_hash``) survives backend restarts —
            # critical for OAuth-mode upstreams whose readiness is
            # purely token-based.
            connection_store=self._connection_repo,
        )
        client_manager.set_on_upstream_tools_changed(
            self._wrap_for_org(org_id),
        )
        client_manager.set_on_upstream_resources_changed(
            self._wrap_resources_for_org(org_id),
        )
        client_manager.set_on_upstream_prompts_changed(
            self._wrap_prompts_for_org(org_id),
        )
        tool_registry = ToolRegistry(
            upstreams,
            client_manager,
            catalog_repo=self._tool_catalog_repo,
            org_id=org_id,
        )
        policy_engine = PolicyEngine(config)
        config_service = UpstreamConfigService(
            self._upstream_config_repo,
            client_manager,
            tool_registry,
            self._connection_repo,
            template_var_repo=self._template_var_repo,
            sandbox_file_repo=self._sandbox_file_repo,
        )
        tool_router = ToolRouter(
            tool_registry,
            client_manager,
            self._audit_repo,
            upstreams,
            policy_engine=policy_engine,
            connection_store=self._connection_repo,
            server_url=self._server_url,
        )
        config_service.set_tool_router(tool_router)

        runtime = OrgRuntime(
            org_id=org_id,
            policy_engine=policy_engine,
            tool_registry=tool_registry,
            client_manager=client_manager,
            tool_router=tool_router,
            config_service=config_service,
            upstreams=upstreams,
        )
        self._runtimes[org_id] = runtime
        # Runtimes start in the "ready" state. connect_runtime() owns the
        # not-ready window; until it runs, nothing is connecting, so the
        # frontend should not show a startup spinner.
        self._startup_status[org_id] = StartupStatus(total=len(upstreams), ready=True)
        admin_emails = policy_engine.get_admin_emails()
        non_admin_count = len(config.users) - len(admin_emails)
        logger.info(
            "org.runtime.initialized",
            org_id=org_id,
            upstream_count=len(upstreams),
            role_count=len(config.roles),
            admin_emails=admin_emails,
            non_admin_user_count=non_admin_count,
        )
        return runtime

    async def _create_runtime_async(
        self,
        org_id: str,
    ) -> OrgRuntime:
        """Load config + upstreams from repos and build a runtime.

        Must be called under ``self._lock``.
        """
        config = await self._config_repo.load(org_id)
        upstreams = await self._upstream_config_repo.get_all(org_id)
        return self._build_runtime(org_id, config, upstreams)
