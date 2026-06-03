from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx
import structlog
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.sandbox_services import (
    LocalSubprocessSandboxService,
)
from mcpolis.adapters.upstream_clients.http_adapter import HttpConnectionTask
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer
from mcpolis.adapters.upstream_clients.log_buffer_region import LogBufferRegion
from mcpolis.adapters.upstream_clients.stdio_adapter import (
    SandboxConnectionTask,
)
from mcpolis.adapters.upstream_clients.upstream_state import (
    UpstreamConnectionState,
    UpstreamState,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import (
    ServerInfo,
    TransportType,
    UpstreamDefinition,
    UpstreamSelfDescription,
)
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
)
from mcpolis.domain.ports import ADMIN_USER_ID
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.system_variables import (
    system_variables_for_sandbox,
)
from mcpolis.domain.services.template_var_substitution import (
    find_placeholders,
    make_layered_resolver,
    substitute_mapping,
    substitute_sequence,
    substitute_string,
)
from mcpolis.domain.services.sandbox_resolver import SandboxResolver
from mcpolis.domain.services.upstream_runtime_hash import (
    compute_upstream_runtime_hash,
)
from mcpolis.domain.services.sandbox_service import (
    MaterializeFile,
    SandboxCapabilities,
    SandboxProviderName,
    SandboxResources,
    SandboxService,
    SnapshotRef,
)


OnUpstreamToolsChanged = Callable[[str], None]
OnUpstreamResourcesChanged = Callable[[str], None]
OnUpstreamPromptsChanged = Callable[[str], None]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

__all__ = ["ADMIN_USER_ID", "UpstreamClientManager"]

# Per-user session idle timeout
USER_SESSION_IDLE_TIMEOUT = 30 * 60  # 30 minutes
USER_SESSION_SWEEP_INTERVAL = 5 * 60  # 5 minutes

ConnectionTask = SandboxConnectionTask | HttpConnectionTask


def _resources_for(upstream: UpstreamDefinition) -> SandboxResources:
    """Build a ``SandboxResources`` from the upstream's stdio config.

    Falls back to the universal default when the upstream isn't a
    stdio upstream (the resources are then unused — HTTP transports
    don't go through any sandbox). The conversion is per-call rather
    than cached so a config edit takes effect on the next session
    without requiring a restart.
    """
    cfg = upstream.stdio
    if cfg is None:
        return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)
    return SandboxResources(
        cpu_vcpus=cfg.cpu_vcpus,
        memory_mb=cfg.memory_mb,
        disk_gb=cfg.disk_gb,
        pids_limit=cfg.pids_limit,
    )


class UpstreamClientManager:
    """Manages long-lived MCP client sessions to upstream servers.

    Three kinds of sessions, intentionally kept on different storage
    so lifecycle rules don't leak across categories:

    * **Upstream-level** (``_state[upstream_id]``): one
      :class:`UpstreamState` record per upstream id. Carries the
      shared discovery session AND the admin OAuth session (if any),
      cached metadata, in-flight reconnect task, last failure context,
      and the lifecycle phase (:class:`UpstreamConnectionState`).
      Single source of truth for "what state is this upstream in?";
      every accessor and every mutation goes through it.
    * **Per-user** (``_user_sessions``): one per ``(user_id,
      upstream_id)``, for ``per_user_oauth`` upstreams. Idle-swept
      after ``USER_SESSION_IDLE_TIMEOUT`` — users log in, use a
      tool, walk away, resources freed. Orthogonal to upstream-level
      state because per-user sessions are personal artefacts of
      individual sign-in, not properties of the upstream itself.
    * **Log buffers** (``self.log_buffers``, a :class:`LogBufferRegion`):
      captured stderr per stdio upstream. Lifecycle outlives session
      reconnects — kept across transitions so the admin can read logs
      after a crash.

    The state record's only mutation surface is the
    ``transition_to_*`` methods (``transition_to_disabled``,
    ``transition_to_failed``, ``transition_to_deferred_attach``,
    ``transition_to_connecting``, ``transition_to_live_shared``,
    ``transition_to_live_admin``) plus the close helpers
    (``_close_shared_inplace``, ``_close_admin_inplace``). External
    code should never poke ``_state`` directly — every reader has a
    typed accessor (``is_connected``, ``is_starting``,
    ``ready_upstream_ids``, etc.) on the manager.
    """

    def __init__(
        self,
        upstreams: list[UpstreamDefinition],
        org_id: str = "default",
        sandbox_resolver: SandboxResolver | None = None,
        sandbox_services: dict[SandboxProviderName, SandboxService] | None = None,
        sandbox_persistence: SandboxPersistenceRepository | None = None,
        mcpolis_instance: str | None = None,
        template_var_repo: TemplateVarRepository | None = None,
        sandbox_file_repo: SandboxFileRepository | None = None,
        connection_store: ConnectionStore | None = None,
    ) -> None:
        self._upstreams = {u.id: u for u in upstreams}
        self._sandbox_persistence = sandbox_persistence
        self._mcpolis_instance = mcpolis_instance
        # Persistent ``started_config_hash`` lives in connection_store
        # so the dashboard's dirty banner survives a backend restart
        # for OAuth upstreams (where ``ready=true`` is purely token-
        # based and no in-memory ``UpstreamState.started_config_hash``
        # ever gets written for the new process). ``None`` ⇔ test
        # factory that doesn't care about persistence; the in-memory
        # ``state.started_config_hash`` then carries the value.
        self._connection_store: ConnectionStore | None = connection_store
        # ``template_var_repo`` resolves ``${NAME}`` references in stdio
        # ``env`` and HTTP ``headers`` at task-start time. ``None``
        # means "no substitution layer wired" — used by the legacy
        # test factories that don't care about env vars; any
        # reference in env/headers then surfaces as a
        # ``MissingTemplateVarError`` at the call site, which is the
        # correct fail-closed behaviour.
        self._template_var_repo = template_var_repo
        # Per-MCP Sandbox files. ``None`` keeps the legacy test
        # factories happy; the cloud / standalone factories always
        # thread one through. When wired, the manager resolves
        # ``${HOME}`` system variables, materialises files via the
        # SandboxService at session start, and exposes the resolved
        # absolute paths in the ``${...}`` namespace so user
        # Variables can reference them.
        self._sandbox_file_repo = sandbox_file_repo
        # Default to ``local-subprocess`` only when neither resolver
        # nor services were supplied (test factories take the easy
        # path). Cloud / standalone deploys always thread these
        # through from ``_build_sandbox_provider_plumbing``.
        self._sandbox_services: dict[SandboxProviderName, SandboxService] = (
            sandbox_services
            if sandbox_services is not None
            else {"local-subprocess": LocalSubprocessSandboxService()}
        )
        self._sandbox_resolver: SandboxResolver = (
            sandbox_resolver
            if sandbox_resolver is not None
            else SandboxResolver(
                global_provider=next(iter(self._sandbox_services)),
            )
        )
        # Threaded through to the SandboxService so per-org metadata
        # tagging (E2B sandbox metadata, persistence keys) is correct.
        self._org_id = org_id

        # ── Primary state: one record per upstream ─────────────
        # Every known upstream starts in FAILED(last_failure=None)
        # — "registered, never connected." Boot reconciler / admin
        # actions transition out of it. Every reader queries this
        # dict (via the accessors); every writer goes through one
        # of the ``transition_to_*`` methods.
        self._state: dict[str, UpstreamState] = {
            uid: UpstreamState(
                state=UpstreamConnectionState.FAILED,
                last_failure=None,
            )
            for uid in self._upstreams
        }

        # ── Orthogonal: per-user OAuth sessions ────────────────
        # Keyed by ``(user_id, upstream_id)``. Idle-swept. Admin
        # sessions never land here — they're upstream-level state
        # (in ``_state[uid].admin_session``) so they survive the
        # idle sweep and user logout.
        self._user_sessions: dict[tuple[str, str], ClientSession] = {}
        self._user_tasks: dict[tuple[str, str], ConnectionTask] = {}
        self._user_session_last_used: dict[tuple[str, str], float] = {}
        self._sweep_task: asyncio.Task[None] | None = None
        # Per-key serialization for ``connect_upstream_for_user``.
        # Two concurrent callers for the same ``(user, upstream)``
        # would otherwise race their disconnect+create sequences and
        # silently leak the loser's task (overwritten in the dicts
        # without anyone awaiting its ``close()``). With the lock,
        # the second caller's ``disconnect_user_session`` observes
        # the first caller's stored task and closes it cleanly
        # before creating the replacement — preserving "the latest
        # connect wins, prior session torn down" semantics.
        self._user_connect_locks: dict[tuple[str, str], asyncio.Lock] = {}

        # ── Orthogonal: stderr capture for stdio upstreams ─────
        # Kept across reconnects so the admin can read crash logs.
        # Storage + lifecycle live behind the ``LogBufferRegion``
        # facade (internal/plans/manager-region-split.md, Phase 1).
        self.log_buffers = LogBufferRegion()

        # ── Orthogonal: single-flight lazy-attach infra ─────────
        # When boot deferred ``connect_shared`` (because the
        # persisted ref carried cached metadata), the first
        # concurrent caller of ``ensure_shared_connected`` creates a
        # task and parks it here; siblings await it instead of
        # stampeding ``Sandbox.connect`` for the same upstream.
        # Cleared on success or failure. NOT a state — the lazy
        # attach is a transition mechanism that runs while the
        # upstream is in DEFERRED_ATTACH and lands it in LIVE
        # (success) or FAILED (failure) without going through
        # CONNECTING — ``CONNECTING`` is reserved for admin-clicked
        # Reconnect, which is cross-tab visible.
        self._lazy_connect_tasks: dict[str, asyncio.Task[None]] = {}

        # Optional callbacks invoked when an upstream reports that its
        # tools / resources / prompts list has changed. Wired from above
        # (org runtime) so the notifier/registry layers stay decoupled
        # from the transport.
        self._on_upstream_tools_changed: OnUpstreamToolsChanged | None = None
        self._on_upstream_resources_changed: OnUpstreamResourcesChanged | None = None
        self._on_upstream_prompts_changed: OnUpstreamPromptsChanged | None = None

    # ── Notification callback wiring ──────────────────────────────

    def set_on_upstream_tools_changed(
        self, callback: OnUpstreamToolsChanged | None,
    ) -> None:
        """Register a callback fired on ``notifications/tools/list_changed``.

        Only newly-created connections pick up the callback — existing
        sessions keep the handler they were constructed with. Wire this
        before ``start_all`` / ``connect_upstream`` calls.
        """
        self._on_upstream_tools_changed = callback

    def set_on_upstream_resources_changed(
        self, callback: OnUpstreamResourcesChanged | None,
    ) -> None:
        """Register a callback fired on
        ``notifications/resources/list_changed``. Same wiring rules as
        ``set_on_upstream_tools_changed``."""
        self._on_upstream_resources_changed = callback

    def set_on_upstream_prompts_changed(
        self, callback: OnUpstreamPromptsChanged | None,
    ) -> None:
        """Register a callback fired on
        ``notifications/prompts/list_changed``."""
        self._on_upstream_prompts_changed = callback

    def _build_tool_change_cb(
        self, upstream_id: str,
    ) -> Callable[[], None] | None:
        cb = self._on_upstream_tools_changed
        if cb is None:
            return None
        return lambda: cb(upstream_id)

    def _build_resource_change_cb(
        self, upstream_id: str,
    ) -> Callable[[], None] | None:
        cb = self._on_upstream_resources_changed
        if cb is None:
            return None
        return lambda: cb(upstream_id)

    def _build_prompt_change_cb(
        self, upstream_id: str,
    ) -> Callable[[], None] | None:
        cb = self._on_upstream_prompts_changed
        if cb is None:
            return None
        return lambda: cb(upstream_id)

    # ─────────────────────────────────────────────────────────────
    # State machine — internal mutation surface
    # ─────────────────────────────────────────────────────────────

    def _log_transition(
        self,
        upstream_id: str,
        from_state: UpstreamConnectionState | None,
        to_state: UpstreamConnectionState,
        **extra: object,
    ) -> None:
        """Log a structured event for every state transition.

        One log line per transition gives operators a per-upstream
        timeline they can grep (``upstream.state.transition
        upstream_id=foo``). The ``from_state`` / ``to_state`` pair
        lets you see "boot found cache → DEFERRED_ATTACH" or "admin
        Reconnect → CONNECTING → LIVE" as a sequence. ``extra``
        carries reason codes and slot kinds (``shared`` / ``admin``)
        for transitions that affect a specific session.
        """
        logger.info(
            "upstream.state.transition",
            upstream_id=upstream_id,
            from_state=from_state.value if from_state is not None else None,
            to_state=to_state.value,
            **extra,
        )

    def _recompute_state_after_session_drop(
        self,
        state: UpstreamState,
        *,
        drop_shared: bool,
        drop_admin: bool,
    ) -> UpstreamConnectionState:
        """Return the lifecycle phase that *would* result from
        dropping the named session(s) on ``state`` while preserving
        all other slots.

        The decision tree:

        1. A non-done ``background_task`` keeps the upstream in
           CONNECTING regardless of session changes (admin clicked
           Reconnect — cross-tab visibility wins).
        2. If any session remains → LIVE.
        3. Else if cached metadata (server_info AND self_description)
           is present → DEFERRED_ATTACH (the cache satisfies the
           dashboard's Ready pill; the next tool dispatch will
           reattach lazily).
        4. Else preserve DISABLED (admin-set), otherwise FAILED.

        Used by ``_close_shared_inplace`` / ``_close_admin_inplace``
        to compute the post-drop state.
        """
        if (
            state.background_task is not None
            and not state.background_task.done()
        ):
            return UpstreamConnectionState.CONNECTING
        shared_present = state.shared_session is not None and not drop_shared
        admin_present = state.admin_session is not None and not drop_admin
        if shared_present or admin_present:
            return UpstreamConnectionState.LIVE
        if (
            state.server_info is not None
            and state.self_description is not None
        ):
            return UpstreamConnectionState.DEFERRED_ATTACH
        if state.state == UpstreamConnectionState.DISABLED:
            return UpstreamConnectionState.DISABLED
        return UpstreamConnectionState.FAILED

    async def _safe_close_task(
        self,
        task: ConnectionTask | None,
        kind: str,
        upstream_id: str,
    ) -> None:
        """Close a connection task; log + swallow exceptions.

        Used by transitions that drop a session — close failures
        shouldn't block state-machine progress, so they're recorded
        and elided. ``kind`` is ``"shared"`` or ``"admin"`` for
        log-grep readability.
        """
        if task is None:
            return
        try:
            await task.close()
        except Exception:
            logger.exception(
                "upstream.client.task.close.failed",
                upstream_id=upstream_id,
                kind=kind,
            )

    async def _drain_state_resources(
        self,
        upstream_id: str,
        old: UpstreamState | None,
    ) -> None:
        """Cancel + close every task referenced by ``old``.

        Used by transitions that drop the entire prior state record
        (``transition_to_disabled``, ``transition_to_failed``,
        ``transition_to_deferred_attach``). Idempotent — safe on a
        record that's already been drained, or on ``None``.
        """
        if old is None:
            return
        bg = old.background_task
        if bg is not None and not bg.done():
            bg.cancel()
            try:
                await bg
            except (asyncio.CancelledError, Exception):
                pass
        await self._safe_close_task(old.shared_task, "shared", upstream_id)
        await self._safe_close_task(old.admin_task, "admin", upstream_id)

    async def transition_to_disabled(
        self,
        upstream_id: str,
        *,
        last_failure: str | None = None,
        reason: str = "admin_disconnect",
    ) -> None:
        """Tear down all sessions, mark the upstream DISABLED.

        Triggered by:

        - admin-initiated Stop (``disconnect_upstream``).
        - admin-initiated upstream removal (``unregister_upstream``).
        - kill-switch enforcement (``kill_all_for_upstream``).
        - auto-disable-on-failure in the boot reconciler — pass
          ``last_failure`` so the dashboard can surface why.

        Drops cached metadata too: a DISABLED upstream has no live
        session AND should not be served from cache (the admin
        explicitly stopped it; a stale cache rendering Ready would
        be a lie).
        """
        old = self._state.get(upstream_id)
        new = UpstreamState(
            state=UpstreamConnectionState.DISABLED,
            last_failure=last_failure,
        )
        self._state[upstream_id] = new
        self._log_transition(
            upstream_id,
            old.state if old is not None else None,
            UpstreamConnectionState.DISABLED,
            reason=reason,
            last_failure=last_failure,
        )
        await self._drain_state_resources(upstream_id, old)
        # Drained state may have left a persisted live ref behind —
        # specifically when ``old`` was DEFERRED_ATTACH (no in-memory
        # task for ``_session_cm.finally`` to clean up via). Fan out
        # to every sandbox provider so the next Start cold-creates a
        # fresh sandbox instead of reattaching to the surviving one.
        await self.kill_persisted_session_for_upstream(upstream_id)

    async def transition_to_failed(
        self,
        upstream_id: str,
        *,
        last_failure: str | None = None,
        reason: str = "connect_failed",
    ) -> None:
        """Tear down all sessions, mark the upstream FAILED.

        Distinct from DISABLED: FAILED says "we tried, it didn't
        work" (or "registered but never connected" when
        ``last_failure`` is None), while DISABLED says "admin chose
        to stop it." The persistence layer's ``enabled:`` key tracks
        the latter; FAILED is purely in-memory.

        Preserves ``server_info`` / ``self_description`` if cached —
        a transient connect failure shouldn't lose the metadata, so
        the next attempt can render the dashboard from cache while
        retrying.
        """
        old = self._state.get(upstream_id)
        new = UpstreamState(
            state=UpstreamConnectionState.FAILED,
            server_info=old.server_info if old is not None else None,
            self_description=old.self_description if old is not None else None,
            last_failure=last_failure,
        )
        self._state[upstream_id] = new
        self._log_transition(
            upstream_id,
            old.state if old is not None else None,
            UpstreamConnectionState.FAILED,
            reason=reason,
            last_failure=last_failure,
        )
        await self._drain_state_resources(upstream_id, old)

    async def compute_runtime_hash(
        self, upstream: UpstreamDefinition,
    ) -> str:
        """Snapshot the inputs that decide an upstream's runtime behaviour.

        Read by the dashboard's detail handler to decide whether the
        running session has drifted from the persisted config. The
        hash itself is opaque to callers — equality is the only
        operation that matters.

        Reads env-var summaries (not plaintext) so the hash never
        carries secret material; the per-row ``updated_at`` stands in
        for value changes since every replace / delete bumps it.
        """
        if self._template_var_repo is None:
            summaries = []
        else:
            summaries = await self._template_var_repo.list_summaries(
                self._org_id, upstream.id,
            )
        # Sandbox files participate in the runtime hash too — a file
        # rename, target_path edit, or contents replacement should
        # surface as "dirty" on the detail page just like a Variable
        # edit. Empty list when no repo is wired (legacy test
        # factories) preserves the historical hash for upstreams that
        # have never used files.
        if self._sandbox_file_repo is None:
            file_summaries = []
        else:
            file_summaries = await self._sandbox_file_repo.list_summaries(
                self._org_id, upstream.id,
            )
        return compute_upstream_runtime_hash(
            upstream, summaries, file_summaries,
        )

    async def get_started_config_hash(
        self, upstream_id: str,
    ) -> str | None:
        """Return the config hash captured when this upstream was last
        running against its persisted config.

        ``None`` ⇔ never started (no snapshot yet). The dashboard
        compares this against a live recompute to drive the "stop &
        restart" dirty banner.

        Reads the persistent ``connection_store`` first so the value
        survives a backend restart — load-bearing for OAuth-mode
        upstreams whose readiness is computed from token existence,
        independently of any in-memory ``UpstreamState`` session.
        Falls back to the in-memory cache for the test factories that
        construct without a connection_store.
        """
        if self._connection_store is not None:
            persisted = await self._connection_store.get_started_config_hash(
                self._org_id, upstream_id,
            )
            if persisted is not None:
                return persisted
        state = self._state.get(upstream_id)
        if state is None:
            return None
        return state.started_config_hash

    async def _persist_started_config_hash(
        self, upstream_id: str, config_hash: str,
    ) -> None:
        """Mirror ``state.started_config_hash`` into connection_store.

        Called from every place that writes started_config_hash to
        UpstreamState so the persistent record stays in sync. No-op
        when constructed without a connection_store (test factories).
        """
        if self._connection_store is None:
            return
        try:
            await self._connection_store.set_started_config_hash(
                self._org_id, upstream_id, config_hash,
            )
        except Exception:
            # The dirty banner degrades gracefully on storage failure
            # (falls back to in-memory cache for this process); a
            # transient store hiccup must not break session creation.
            logger.warning(
                "upstream.client.persist_started_config_hash.failed",
                upstream_id=upstream_id, exc_info=True,
            )

    async def transition_to_deferred_attach(
        self,
        upstream_id: str,
        *,
        server_info: ServerInfo,
        self_description: UpstreamSelfDescription,
        started_config_hash: str | None = None,
    ) -> None:
        """Mark the upstream DEFERRED_ATTACH with cached metadata.

        From the user's POV the upstream is Ready — the cache
        satisfies dashboard reads. The next tool dispatch reattaches
        lazily via ``ensure_shared_connected`` (which transitions to
        LIVE on success, FAILED on failure).

        Tears down any prior sessions: DEFERRED_ATTACH means "no
        live session, only cache." Used at boot when persistence
        carries the cached fields.
        """
        old = self._state.get(upstream_id)
        new = UpstreamState(
            state=UpstreamConnectionState.DEFERRED_ATTACH,
            server_info=server_info,
            self_description=self_description,
            started_config_hash=(
                started_config_hash
                if started_config_hash is not None
                else (old.started_config_hash if old is not None else None)
            ),
        )
        self._state[upstream_id] = new
        self._log_transition(
            upstream_id,
            old.state if old is not None else None,
            UpstreamConnectionState.DEFERRED_ATTACH,
        )
        await self._drain_state_resources(upstream_id, old)

    def transition_to_connecting(
        self,
        upstream_id: str,
        *,
        background_task: asyncio.Task[None],
    ) -> None:
        """Mark the upstream CONNECTING (admin Reconnect in flight).

        Sync — the caller has already created the background task
        and just needs the manager to track it. Drives the
        dashboard's "Starting…" disabled-button state across tabs.

        Preserves cached metadata so dashboard reads still resolve
        while the reconnect is in flight, and clears any stale
        ``last_failure`` so a successful reconnect surfaces clean
        state.

        Cancels any prior in-flight background task for the same
        upstream so a re-click of Start doesn't end up racing two
        warming sandboxes against each other.
        """
        old = self._state.get(upstream_id)
        new = UpstreamState(
            state=UpstreamConnectionState.CONNECTING,
            shared_session=old.shared_session if old is not None else None,
            shared_task=old.shared_task if old is not None else None,
            admin_session=old.admin_session if old is not None else None,
            admin_task=old.admin_task if old is not None else None,
            server_info=old.server_info if old is not None else None,
            self_description=old.self_description if old is not None else None,
            background_task=background_task,
            last_failure=None,
        )
        self._state[upstream_id] = new
        self._log_transition(
            upstream_id,
            old.state if old is not None else None,
            UpstreamConnectionState.CONNECTING,
        )
        if (
            old is not None
            and old.background_task is not None
            and old.background_task is not background_task
            and not old.background_task.done()
        ):
            old.background_task.cancel()

    def transition_to_live_shared(
        self,
        upstream_id: str,
        *,
        session: ClientSession,
        task: ConnectionTask,
        server_info: ServerInfo | None,
        self_description: UpstreamSelfDescription | None,
        started_config_hash: str | None = None,
    ) -> None:
        """Record a freshly-opened shared session, advance to LIVE.

        Sync — the caller has already awaited ``_create_task`` and
        just needs the state machine updated. Preserves any admin
        session that was already in place (OAuth upstreams can have
        both shared discovery + admin authenticated simultaneously).

        Clears ``background_task`` (the connect succeeded) and
        ``last_failure`` (stale failure context after a successful
        connect would be misleading).

        Caller contract: any prior ``shared_task`` should already
        have been closed (via ``_close_shared_inplace`` in the
        close-then-open sequence). If a stray prior task is
        observed, it's closed in the background as belt-and-braces.
        """
        old = self._state.get(upstream_id)
        new = UpstreamState(
            state=UpstreamConnectionState.LIVE,
            shared_session=session,
            shared_task=task,
            admin_session=old.admin_session if old is not None else None,
            admin_task=old.admin_task if old is not None else None,
            server_info=(
                server_info
                if server_info is not None
                else (old.server_info if old is not None else None)
            ),
            self_description=(
                self_description
                if self_description is not None
                else (old.self_description if old is not None else None)
            ),
            background_task=None,
            last_failure=None,
            started_config_hash=(
                started_config_hash
                if started_config_hash is not None
                else (old.started_config_hash if old is not None else None)
            ),
        )
        self._state[upstream_id] = new
        self._log_transition(
            upstream_id,
            old.state if old is not None else None,
            UpstreamConnectionState.LIVE,
            session_kind="shared",
        )
        if (
            old is not None
            and old.shared_task is not None
            and old.shared_task is not task
        ):
            asyncio.create_task(
                self._safe_close_task(old.shared_task, "shared", upstream_id),
                name=f"close_orphan_shared_{upstream_id}",
            )

    def transition_to_live_admin(
        self,
        upstream_id: str,
        *,
        session: ClientSession,
        task: ConnectionTask,
        server_info: ServerInfo | None,
        self_description: UpstreamSelfDescription | None,
        started_config_hash: str | None = None,
    ) -> None:
        """Record a freshly-opened admin OAuth session, advance to LIVE.

        Symmetric to ``transition_to_live_shared`` — preserves any
        shared session already in place. Used by
        ``connect_admin_session`` after a successful OAuth-bound
        connect.
        """
        old = self._state.get(upstream_id)
        new = UpstreamState(
            state=UpstreamConnectionState.LIVE,
            shared_session=old.shared_session if old is not None else None,
            shared_task=old.shared_task if old is not None else None,
            admin_session=session,
            admin_task=task,
            server_info=(
                server_info
                if server_info is not None
                else (old.server_info if old is not None else None)
            ),
            self_description=(
                self_description
                if self_description is not None
                else (old.self_description if old is not None else None)
            ),
            background_task=None,
            last_failure=None,
            started_config_hash=(
                started_config_hash
                if started_config_hash is not None
                else (old.started_config_hash if old is not None else None)
            ),
        )
        self._state[upstream_id] = new
        self._log_transition(
            upstream_id,
            old.state if old is not None else None,
            UpstreamConnectionState.LIVE,
            session_kind="admin",
        )
        if (
            old is not None
            and old.admin_task is not None
            and old.admin_task is not task
        ):
            asyncio.create_task(
                self._safe_close_task(old.admin_task, "admin", upstream_id),
                name=f"close_orphan_admin_{upstream_id}",
            )

    async def _close_shared_inplace(self, upstream_id: str) -> None:
        """Close the shared session (if any), recompute state.

        Used in the close-then-open sequence inside
        ``connect_shared`` — frees the sandbox slot before the next
        ``Sandbox.connect`` so we never run two sandboxes for the
        same upstream concurrently.

        After close: if ``admin_session`` is still live the upstream
        stays LIVE; else if cached metadata is present it falls back
        to DEFERRED_ATTACH; else it becomes FAILED (or stays
        DISABLED, if it already was).
        """
        old = self._state.get(upstream_id)
        if (
            old is None
            or (old.shared_session is None and old.shared_task is None)
        ):
            return
        new_enum = self._recompute_state_after_session_drop(
            old, drop_shared=True, drop_admin=False,
        )
        new = UpstreamState(
            state=new_enum,
            shared_session=None,
            shared_task=None,
            admin_session=old.admin_session,
            admin_task=old.admin_task,
            server_info=old.server_info,
            self_description=old.self_description,
            background_task=old.background_task,
            last_failure=old.last_failure,
        )
        self._state[upstream_id] = new
        self._log_transition(
            upstream_id, old.state, new_enum,
            session_kind="shared", action="closed",
        )
        await self._safe_close_task(
            old.shared_task, "shared", upstream_id,
        )
        if old.shared_session is not None:
            logger.info(
                "upstream.client.shared_session.closed",
                upstream_id=upstream_id,
            )

    async def _close_admin_inplace(self, upstream_id: str) -> None:
        """Close the admin session (if any), recompute state.

        Symmetric to ``_close_shared_inplace``.
        """
        old = self._state.get(upstream_id)
        if (
            old is None
            or (old.admin_session is None and old.admin_task is None)
        ):
            return
        new_enum = self._recompute_state_after_session_drop(
            old, drop_shared=False, drop_admin=True,
        )
        new = UpstreamState(
            state=new_enum,
            shared_session=old.shared_session,
            shared_task=old.shared_task,
            admin_session=None,
            admin_task=None,
            server_info=old.server_info,
            self_description=old.self_description,
            background_task=old.background_task,
            last_failure=old.last_failure,
        )
        self._state[upstream_id] = new
        self._log_transition(
            upstream_id, old.state, new_enum,
            session_kind="admin", action="closed",
        )
        await self._safe_close_task(
            old.admin_task, "admin", upstream_id,
        )
        if old.admin_session is not None:
            logger.info(
                "upstream.client.admin_session.closed",
                upstream_id=upstream_id,
            )

    # ─────────────────────────────────────────────────────────────
    # Connect / lifecycle
    # ─────────────────────────────────────────────────────────────

    async def connect_shared_or_defer(
        self, upstream: UpstreamDefinition,
    ) -> bool:
        """Phase-1 entry point: open a shared session, or defer to a
        lazy attach when persistence holds the cached metadata.

        Returns ``True`` iff the connect was deferred — caller should
        treat the upstream as "ready (cached, lazy)". Returns ``False``
        when a live shared session was opened. Raises on hard failure
        so callers can record per-upstream failed-state.

        Single source of truth for the boot-skip gate: ``connect_runtime``
        and ``start_all`` and the integration suite all funnel through
        here, so a future refactor can't accidentally reintroduce the
        "the gate is in start_all but boot doesn't go through start_all"
        bug class.
        """
        if await self.try_defer_boot_attach(upstream):
            return True
        await self.connect_shared(upstream)
        return False

    async def try_defer_boot_attach(
        self, upstream: UpstreamDefinition,
    ) -> bool:
        """Attempt to skip ``connect_shared`` for this upstream by
        moving it straight to DEFERRED_ATTACH from cached metadata in
        persistence. Returns ``True`` when the deferral is now in
        place; ``False`` when this upstream can't be deferred (no
        cache, wrong transport, etc.).

        The ``try_`` prefix is load-bearing: this is **not** a pure
        query. On success it transitions the upstream into
        DEFERRED_ATTACH with the cached ``server_info`` /
        ``self_description``, so dashboard reads resolve without a
        live session. (The earlier predicate-shaped name —
        ``can_defer_boot_attach`` — was misread as side-effect-free
        and the call site missed the readiness wiring; this rename
        makes the action half visible at every call site.) Without
        the in-memory transition every readiness surface in the app
        would render the cached upstream as "Stopped".

        On ``True`` the caller MUST skip ``connect_shared`` —
        calling it would invoke ``Sandbox.connect`` +
        ``commands.connect``, both of which auto-resume a paused
        E2B sandbox (defeating the whole point of idle-pause). Tool
        dispatch lazily reattaches via ``ensure_shared_connected``
        on first demand.

        Returns ``False`` when:
        - upstream isn't ``service_account`` stdio (only that path
          goes through the sandbox auto_resume),
        - persistence isn't wired,
        - no ref exists yet (first-ever boot for this upstream),
        - ref pre-dates the metadata-cache feature.
        """
        if upstream.auth.mode != AuthMode.service_account:
            return False
        if upstream.transport != TransportType.stdio:
            return False
        cached = await self._read_cached_metadata(upstream)
        if cached is None:
            return False
        server_info, self_description = cached
        # Snapshot the runtime hash from the persisted config the
        # cached session was started against. If the user's saved
        # state has drifted since boot, the dashboard's dirty banner
        # fires on first read.
        started_config_hash = await self.compute_runtime_hash(upstream)
        await self.transition_to_deferred_attach(
            upstream.id,
            server_info=server_info,
            self_description=self_description,
            started_config_hash=started_config_hash,
        )
        await self._persist_started_config_hash(
            upstream.id, started_config_hash,
        )
        return True

    async def _read_cached_metadata(
        self, upstream: UpstreamDefinition,
    ) -> tuple[ServerInfo, UpstreamSelfDescription] | None:
        """Read the persisted ``server_info`` + ``self_description``
        for an upstream's cached sandbox ref.

        Returns ``None`` when persistence isn't wired, the ref is
        missing, or the ref pre-dates the metadata-cache feature.
        Read errors are logged and treated as missing (the boot
        reconciler degrades to eager connect rather than crashing).
        """
        if self._sandbox_persistence is None:
            return None
        try:
            ref = await self._sandbox_persistence.get(
                org_id=self._org_id, upstream_id=upstream.id,
            )
        except Exception:
            logger.exception(
                "upstream.client.boot.persistence_read_failed",
                upstream_id=upstream.id,
            )
            return None
        if ref is None:
            return None
        if (
            ref.cached_server_info is None
            or ref.cached_self_description is None
        ):
            return None
        return ref.cached_server_info, ref.cached_self_description

    async def start_all(self) -> None:
        # Parallel for the same reason as
        # ``OrgRuntimeManager.connect_runtime`` — see the comment
        # block there. ``connect_shared`` is keyed per upstream id
        # so siblings don't contend; ``return_exceptions=True``
        # protects against an unexpected raise cancelling the rest
        # of the gather.
        async def _connect_one(
            upstream_id: str, upstream: UpstreamDefinition,
        ) -> None:
            if upstream.auth.mode == AuthMode.service_account:
                try:
                    deferred = await self.connect_shared_or_defer(upstream)
                except Exception as exc:
                    await self.transition_to_failed(
                        upstream_id,
                        last_failure=str(exc),
                        reason="start_all_connect_failed",
                    )
                    logger.exception(
                        "upstream.client.connect.failed",
                        upstream_id=upstream_id,
                    )
                    return
                if deferred:
                    logger.info(
                        "upstream.client.boot.deferred_attach",
                        upstream_id=upstream_id,
                    )
                else:
                    logger.info(
                        "upstream.client.connect.success",
                        upstream_id=upstream_id,
                    )
            else:
                # OAuth upstreams: try unauthenticated connection
                # for tool discovery. Many MCP servers allow
                # tools/list without auth.
                try:
                    await self.try_discovery_connect(upstream)
                except Exception:
                    logger.exception(
                        "upstream.client.discovery.failed",
                        upstream_id=upstream_id,
                    )

        await asyncio.gather(
            *(
                _connect_one(uid, u)
                for uid, u in self._upstreams.items()
            ),
            return_exceptions=True,
        )

    async def stop_all(self) -> None:
        """Tear down every session. Manager is unusable afterwards.

        Iterates the per-user dicts (admin sessions and shared
        sessions are inside the state record, so a single pass over
        ``_state`` drains them via ``_drain_state_resources``).
        """
        # Cancel sweep task
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            self._sweep_task = None

        # Clean up per-user sessions (orthogonal storage).
        for key, task in list(self._user_tasks.items()):
            try:
                await task.close()
            except BaseException:
                user_id, upstream_id = key
                logger.warning(
                    "upstream.client.user_task.close.failed_ignored",
                    upstream_id=upstream_id,
                    user=user_id,
                )
        self._user_sessions.clear()
        self._user_tasks.clear()
        self._user_session_last_used.clear()

        # Clean up upstream-level state (shared + admin tasks live
        # in the state record).
        for upstream_id, state in list(self._state.items()):
            try:
                await self._drain_state_resources(upstream_id, state)
            except BaseException:
                logger.warning(
                    "upstream.client.state.drain.failed_ignored",
                    upstream_id=upstream_id,
                )
        self._state.clear()

    async def _resolve_sandbox_files(
        self, upstream: UpstreamDefinition,
    ) -> list[MaterializeFile]:
        """Render every Sandbox file's ``target_path`` against system
        + user Variables and return the materialise list for the
        launcher.

        ``target_path`` accepts the same ``${...}`` references that
        env-var values, command, args, url, and headers accept —
        system Variables (``${HOME}`` and future entries) plus user
        Variables on the same upstream. Unknown tokens raise
        :class:`MissingTemplateVarError`. Cycles are structurally
        impossible: files don't export symbols into the substitution
        namespace, so user vars can reference system vars but not
        files, and target_paths can reference system + user vars but
        not other files.

        Returns an empty list when no Sandbox-files repo is wired
        (legacy test factories) or when the upstream has no files.
        """
        if self._sandbox_file_repo is None or upstream.transport != TransportType.stdio:
            return []
        files = await self._sandbox_file_repo.list_full(
            self._org_id, upstream.id,
        )
        if not files:
            return []
        sys_vars = system_variables_for_sandbox()

        # Pre-fetch every user-var referenced in any target_path so
        # the sync resolver doesn't have to ``await`` per match.
        # Tokens that match a system Variable are served from
        # ``sys_vars`` and don't trigger a repo round-trip.
        referenced: set[str] = set()
        for f in files:
            referenced.update(find_placeholders(f.target_path))
        user_resolved: dict[str, str] = {}
        if self._template_var_repo is not None:
            for name in referenced:
                if name in sys_vars:
                    continue
                value = await self._template_var_repo.get_value(
                    self._org_id, upstream.id, name,
                )
                if value is not None:
                    user_resolved[name] = value

        resolver = make_layered_resolver(sys_vars, user_resolved)
        materialized: list[MaterializeFile] = []
        for f in files:
            resolved_path = substitute_string(
                f.target_path,
                resolver=resolver,
                upstream_id=upstream.id,
            )
            materialized.append(
                MaterializeFile(
                    name=f.name,
                    contents=f.contents,
                    target_path=resolved_path,
                ),
            )
        return materialized

    async def _resolve_upstream_template_vars(
        self, upstream: UpstreamDefinition
    ) -> UpstreamDefinition:
        """Return a copy of ``upstream`` with ``${NAME}`` refs resolved.

        Substitutes every user-controlled string field on the
        upstream:

        - stdio: ``command``, ``args`` (each element), ``env`` values.
        - http: ``url``, ``headers`` values.

        ``\\${NAME}`` escapes the substitution: the backslash is
        consumed and the rest emits as a literal ``${NAME}``. Useful
        when the downstream tool's own syntax (e.g. an inline Python
        ``-c`` snippet that references a real environment variable)
        looks like our placeholder.

        If no ``template_var_repo`` was injected (legacy tests), the
        upstream is returned unchanged — any ``${NAME}`` reference
        will then surface as a ``MissingTemplateVarError`` at substitution
        time, which is the desired fail-closed behaviour.

        Do NOT log the returned object — its fields carry plaintext
        values (possibly secret) after substitution. Enforced
        statically by ``test_no_env_logging``.
        """
        if self._template_var_repo is None:
            return upstream

        async def _resolve(name: str) -> str | None:
            assert self._template_var_repo is not None  # narrowed for pyright
            return await self._template_var_repo.get_value(
                self._org_id, upstream.id, name,
            )

        # The substitution helper is sync and takes a sync resolver;
        # pre-fetch every referenced env var in one async pass so the
        # helper itself doesn't have to ``await`` per match.
        from mcpolis.domain.services.template_var_substitution import (  # noqa: PLC0415
            find_placeholders,
        )

        referenced: set[str] = set()
        if upstream.stdio is not None:
            referenced.update(find_placeholders(upstream.stdio.command))
            for arg in upstream.stdio.args:
                referenced.update(find_placeholders(arg))
            for value in upstream.stdio.env.values():
                referenced.update(find_placeholders(value))
        if upstream.http is not None:
            referenced.update(find_placeholders(upstream.http.url))
            for value in upstream.http.headers.values():
                referenced.update(find_placeholders(value))

        # Resolution: ``${...}`` resolves against (system + user)
        # Variables. Sandbox files don't export symbols into this
        # namespace — operators reference file paths via
        # ``${HOME}/.../path`` literals on both sides (file
        # ``target_path`` and the env-var value), preserving a single
        # mental model for what ``${X}`` means everywhere.
        sys_vars = system_variables_for_sandbox()

        resolved: dict[str, str | None] = {}
        for name in referenced:
            if name in sys_vars:
                resolved[name] = sys_vars[name]
                continue
            resolved[name] = await _resolve(name)

        layered = make_layered_resolver(
            sys_vars,
            {k: v for k, v in resolved.items() if isinstance(v, str)},
        )

        def _sync_resolver(name: str) -> str | None:
            v = layered(name)
            if v is not None:
                return v
            # Preserve the legacy contract: an unresolved name surfaces
            # as ``None`` so substitute_string raises MissingTemplateVarError.
            return resolved.get(name)

        # Refresh the per-upstream log-redaction set so the next
        # stderr write that includes a substituted secret value is
        # masked as ``[REDACTED:NAME]`` before it lands in the
        # operator's Server-logs panel. Plain (is_secret=false)
        # values are operator-visible by design and not redacted.
        # We only consult ``list_summaries`` for the secret flags;
        # values come from the resolution above. List call is cheap
        # (in-memory file repo / single Mongo query).
        if upstream.transport == TransportType.stdio and referenced:
            try:
                summaries = await self._template_var_repo.list_summaries(
                    self._org_id, upstream.id,
                )
            except Exception:
                # Listing must not block session start; without
                # redaction the buffer captures plaintext, which is
                # the pre-existing behaviour.
                summaries = []
            secret_names = {s.name for s in summaries if s.is_secret}
            redactions = {
                value: name
                for name, value in resolved.items()
                if value is not None and name in secret_names
            }
            self.log_buffers.set_redactions(upstream.id, redactions)

        new_stdio: StdioTransportConfig | None = upstream.stdio
        new_http: HttpTransportConfig | None = upstream.http
        if upstream.stdio is not None:
            stdio_updates: dict[str, object] = {}
            new_command = substitute_string(
                upstream.stdio.command,
                resolver=_sync_resolver,
                upstream_id=upstream.id,
            )
            if new_command != upstream.stdio.command:
                stdio_updates["command"] = new_command
            if upstream.stdio.args:
                new_args = substitute_sequence(
                    list(upstream.stdio.args),
                    resolver=_sync_resolver,
                    upstream_id=upstream.id,
                )
                if new_args != list(upstream.stdio.args):
                    stdio_updates["args"] = new_args
            if upstream.stdio.env:
                new_env = substitute_mapping(
                    upstream.stdio.env,
                    resolver=_sync_resolver,
                    upstream_id=upstream.id,
                )
                if new_env != upstream.stdio.env:
                    stdio_updates["env"] = new_env
            if stdio_updates:
                new_stdio = upstream.stdio.model_copy(update=stdio_updates)
        if upstream.http is not None:
            http_updates: dict[str, object] = {}
            new_url = substitute_string(
                upstream.http.url,
                resolver=_sync_resolver,
                upstream_id=upstream.id,
            )
            if new_url != upstream.http.url:
                http_updates["url"] = new_url
            if upstream.http.headers:
                new_headers = substitute_mapping(
                    upstream.http.headers,
                    resolver=_sync_resolver,
                    upstream_id=upstream.id,
                )
                if new_headers != upstream.http.headers:
                    http_updates["headers"] = new_headers
            if http_updates:
                new_http = upstream.http.model_copy(update=http_updates)

        if new_stdio is upstream.stdio and new_http is upstream.http:
            return upstream
        return upstream.model_copy(
            update={"stdio": new_stdio, "http": new_http}
        )

    async def _create_task(
        self,
        upstream: UpstreamDefinition,
        user_id: str,
        bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> tuple[ClientSession, ConnectionTask]:
        # Resolve ``${NAME}`` refs in env / headers before either
        # connection task ever sees them, so the SandboxService and
        # HTTP adapter only ever handle concrete values.
        upstream = await self._resolve_upstream_template_vars(upstream)
        on_tools_changed = self._build_tool_change_cb(upstream.id)
        on_resources_changed = self._build_resource_change_cb(upstream.id)
        on_prompts_changed = self._build_prompt_change_cb(upstream.id)
        if upstream.transport == TransportType.stdio:
            log_buf = self.log_buffers.get_or_create(upstream.id)
            provider = await self._sandbox_resolver.resolve(org_id=self._org_id)
            try:
                service = self._sandbox_services[provider]
            except KeyError as exc:
                raise RuntimeError(
                    f"sandbox provider {provider!r} resolved but not"
                    f" registered; have {sorted(self._sandbox_services)}",
                ) from exc
            resources = _resources_for(upstream)
            service.validate_resources(resources)
            materialize_files = await self._resolve_sandbox_files(upstream)
            task: ConnectionTask = SandboxConnectionTask(
                upstream,
                user_id=user_id,
                service=service,
                resources=resources,
                org_id=self._org_id,
                bearer_token=bearer_token,
                log_buffer=log_buf,
                on_tool_list_changed=on_tools_changed,
                on_resource_list_changed=on_resources_changed,
                on_prompt_list_changed=on_prompts_changed,
                sandbox_persistence=self._sandbox_persistence,
                mcpolis_instance=self._mcpolis_instance,
                materialize_files=materialize_files,
            )
        else:
            task = HttpConnectionTask(
                upstream,
                user_id=user_id,
                bearer_token=bearer_token,
                auth=auth,
                on_tool_list_changed=on_tools_changed,
                on_resource_list_changed=on_resources_changed,
                on_prompt_list_changed=on_prompts_changed,
            )
        session = await task.start()
        return session, task

    async def connect_shared(
        self,
        upstream: UpstreamDefinition,
        bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> None:
        """Connect a shared upstream — close any prior shared
        session, open a fresh one, transition to LIVE.

        The close-then-open order is deliberate: it frees the
        sandbox slot before the next ``Sandbox.connect``, preventing
        two sandboxes from briefly running for the same upstream.
        If creating the new task fails (mid-step), the upstream
        falls into FAILED via the caller's exception handler — the
        prior session is gone so the previously-live upstream is
        no longer usable. That tradeoff is intentional and matches
        the legacy behavior the integration tests pin.
        """
        await self._close_shared_inplace(upstream.id)
        session, task = await self._create_task(
            upstream, user_id="__shared__",
            bearer_token=bearer_token, auth=auth,
        )
        # Snapshot the runtime hash *after* the task has spun up
        # successfully; if the connect failed we'd never reach here.
        started_config_hash = await self.compute_runtime_hash(upstream)
        self.transition_to_live_shared(
            upstream.id,
            session=session,
            task=task,
            server_info=task.server_info,
            self_description=task.self_description,
            started_config_hash=started_config_hash,
        )
        await self._persist_started_config_hash(
            upstream.id, started_config_hash,
        )
        # Persist the freshly observed metadata so a subsequent boot
        # can render the dashboard from cache without ever opening a
        # session (and thus without auto-resuming a paused sandbox).
        await self._persist_cached_metadata(upstream.id)
        logger.info(
            "upstream.client.shared_session.created",
            upstream_id=upstream.id,
        )

    async def reconnect_shared_fresh(
        self,
        upstream: UpstreamDefinition,
        bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> None:
        """Force a FRESH shared session, never a reattach.

        Used to recover from a transport that connected but then went
        silent — most importantly E2B's intermittent post-reattach
        stdout stall, where ``commands.connect`` to a resumed sandbox
        delivers a response or two and then stops. Plain
        ``connect_shared`` would (under reuse-on-restart) reattach to
        that same flaky sandbox via the persisted live ref and stall
        again. Dropping the ref first forces the sandbox service down
        its fresh-create path (new sandbox, new streaming RPC), so the
        retry runs on a clean transport.

        The stale/flaky sandbox is closed by ``connect_shared``'s
        close-then-open sequence and reaped by the reconciler; the cost
        of a fresh create on the rare stall buys a reliable session.
        """
        if self._sandbox_persistence is not None:
            try:
                await self._sandbox_persistence.delete(
                    org_id=self._org_id, upstream_id=upstream.id,
                )
            except Exception:
                logger.warning(
                    "upstream.client.reconnect_fresh.ref_delete_failed",
                    upstream_id=upstream.id,
                    exc_info=True,
                )
        logger.info(
            "upstream.client.reconnect_shared_fresh",
            upstream_id=upstream.id,
        )
        await self.connect_shared(upstream, bearer_token=bearer_token, auth=auth)

    async def _persist_cached_metadata(self, upstream_id: str) -> None:
        """Write ``server_info`` + ``self_description`` back to the
        sandbox persistence ref. Idempotent; merges into the existing
        ref so ``sandbox_id`` / ``pid`` written by the sandbox
        service aren't clobbered.
        """
        if self._sandbox_persistence is None:
            return
        state = self._state.get(upstream_id)
        if state is None:
            return
        server_info = state.server_info
        self_description = state.self_description
        if server_info is None and self_description is None:
            return
        try:
            ref = await self._sandbox_persistence.get(
                org_id=self._org_id, upstream_id=upstream_id,
            )
        except Exception:
            logger.exception(
                "upstream.client.metadata_persist.read_failed",
                upstream_id=upstream_id,
            )
            return
        if ref is None:
            # Nothing to merge into — the sandbox service writes the
            # base ref. We'll persist on the next ``connect_shared``.
            return
        try:
            await self._sandbox_persistence.upsert(
                ref.model_copy(update={
                    "cached_server_info": server_info,
                    "cached_self_description": self_description,
                }),
            )
        except Exception:
            logger.exception(
                "upstream.client.metadata_persist.write_failed",
                upstream_id=upstream_id,
            )

    async def ensure_shared_connected(
        self, upstream: UpstreamDefinition,
    ) -> None:
        """Lazily open a shared session if one isn't already live.

        Called from request-time hot paths (e.g. the tool router)
        after boot deferred ``connect_shared`` for this upstream.
        Single-flight per upstream id: concurrent callers join the
        same in-flight task instead of stampeding ``Sandbox.connect``
        and racing toward the same E2B-side wake.

        Idempotent — when a shared session already exists, returns
        immediately without touching E2B.

        Crucially, lazy attach does NOT transition to CONNECTING:
        it's a request-scoped, in-band reconnect that the user
        experiences as latency on a single tool call, not a
        cross-tab "Starting…" event.
        """
        state = self._state.get(upstream.id)
        if state is not None and state.shared_session is not None:
            # Reuse the existing shared session — UNLESS its transport
            # has fatally died (sandbox expired / unrecoverable
            # reattach). A dead session is still registered and looks
            # present, but every send on it raises BrokenResourceError
            # (and the first post-death request hangs out its timeout).
            # Fall through to a full reconnect (connect_shared, which
            # closes the zombie and opens a fresh sandbox) instead of
            # handing the caller a dead session.
            task = state.shared_task
            if task is None or task.is_transport_alive():
                return
            logger.info(
                "upstream.client.shared_session.dead_reconnecting",
                upstream_id=upstream.id,
            )
        existing = self._lazy_connect_tasks.get(upstream.id)
        if existing is not None and not existing.done():
            await existing
            return

        async def _do_connect() -> None:
            # Total wall-clock for the lazy-attach round-trip. Free
            # to capture (the timer would exist anyway via ad-hoc
            # operator stopwatching) and lets operators trend
            # "is reuse getting slower" without stitching the
            # three component events (envd_ready + reconnect.ok +
            # shared_session.created) by hand.
            started = asyncio.get_running_loop().time()
            try:
                await self.connect_shared(upstream)
                logger.info(
                    "upstream.client.lazy_connect.success",
                    upstream_id=upstream.id,
                    total_duration_ms=int(
                        (asyncio.get_running_loop().time() - started) * 1000,
                    ),
                )
            except Exception as exc:
                # Lazy attach failed: the upstream isn't usable. Mark
                # FAILED so the dashboard refetch shows the truth.
                # The user's tool call will surface the underlying
                # error; the next dispatch retries via this same
                # method.
                await self.transition_to_failed(
                    upstream.id,
                    last_failure=str(exc),
                    reason="lazy_attach_failed",
                )
                logger.exception(
                    "upstream.client.lazy_connect.failed",
                    upstream_id=upstream.id,
                    total_duration_ms=int(
                        (asyncio.get_running_loop().time() - started) * 1000,
                    ),
                )
                raise

        task = asyncio.create_task(_do_connect())
        self._lazy_connect_tasks[upstream.id] = task
        try:
            await task
        finally:
            # Clear the entry only when it still points at our task —
            # protects against a future caller that overwrote it after
            # ours completed.
            if self._lazy_connect_tasks.get(upstream.id) is task:
                self._lazy_connect_tasks.pop(upstream.id, None)

    def get_log_output(self, upstream_id: str) -> str | None:
        """Return captured stderr output for a stdio upstream, or None.

        Facade delegation to :class:`LogBufferRegion`. New callers
        should prefer ``manager.log_buffers.get_output(...)`` directly.
        """
        return self.log_buffers.get_output(upstream_id)

    def get_log_buffer(self, upstream_id: str) -> LogBuffer | None:
        """Return the LogBuffer for a stdio upstream, or None.

        Facade delegation to :class:`LogBufferRegion`. New callers
        should prefer ``manager.log_buffers.get(...)`` directly.
        """
        return self.log_buffers.get(upstream_id)

    async def get_active_capabilities(self) -> SandboxCapabilities:
        """Return the capabilities of the provider currently selected
        for this manager's org. Drives the admin UI's CPU/RAM/disk
        picker."""
        provider = await self._sandbox_resolver.resolve(org_id=self._org_id)
        service = self._sandbox_services[provider]
        return service.capabilities()

    async def kill_persisted_session_for_upstream(
        self, upstream_id: str,
    ) -> None:
        """Kill any persisted live sandbox for ``upstream_id`` without
        destroying its persistent storage.

        Called from :meth:`transition_to_disabled` so a Stop on an
        upstream that's in DEFERRED_ATTACH (post-boot lazy-reattach
        with a persistence ref but no in-memory task) actually kills
        the underlying sandbox. Without this, the next Start's
        reuse-on-restart path Path 2 reattaches to the same sandbox
        and the user sees an empty Server-logs panel because the
        per-session ``LogBuffer.clear()`` ran but no fresh install /
        startup output replaced it.

        Fans out across every registered provider — same idempotency
        contract as :meth:`cleanup_sandbox_state_for_upstream`. Errors
        are logged and swallowed so a transient SDK glitch in one
        backend can't block the disable transition.
        """
        for provider_name, service in self._sandbox_services.items():
            try:
                await service.kill_persisted_session(
                    org_id=self._org_id, upstream_id=upstream_id,
                )
            except Exception:
                logger.warning(
                    "upstream.client.kill_persisted_session.failed",
                    org_id=self._org_id,
                    upstream_id=upstream_id,
                    provider=provider_name,
                    exc_info=True,
                )

    async def cleanup_sandbox_state_for_upstream(
        self, upstream_id: str,
    ) -> None:
        """Tear down provider-side state attached to ``upstream_id``
        when the operator removes the upstream.

        Dispatches to every registered sandbox service so a stale
        volume / persistence ref left behind by a prior provider
        switch still gets cleaned up. Each backend's
        ``on_upstream_removed`` is documented as idempotent — a
        no-op when there's nothing to tear down — so calling all of
        them is safe.

        Failures from individual backends are logged and swallowed
        so a transient SDK error in one provider can't block the
        operator's delete action; the reconciler is the eventual
        consistency net.
        """
        for provider_name, service in self._sandbox_services.items():
            try:
                await service.on_upstream_removed(
                    org_id=self._org_id, upstream_id=upstream_id,
                )
            except Exception:
                logger.warning(
                    "upstream.client.sandbox_cleanup.failed",
                    org_id=self._org_id,
                    upstream_id=upstream_id,
                    provider=provider_name,
                    exc_info=True,
                )

    def get_active_provider_name(self) -> SandboxProviderName | None:
        """Synchronous best-effort accessor for the resolver default.

        Returns the resolver's global default provider, NOT the
        per-org-resolved value — for that, ``await
        get_active_capabilities()`` first. Useful in places where
        the caller can't await (e.g. settings dump).
        """
        provider = getattr(self._sandbox_resolver, "_global_provider", None)
        if not isinstance(provider, str):
            return None
        if provider not in self._sandbox_services:
            return None
        return provider  # type: ignore[return-value]

    async def pause_upstream(self, upstream_id: str) -> SnapshotRef | None:
        """Pause the live shared sandbox session for ``upstream_id``.

        Looks up the active ``SandboxConnectionTask`` for the
        upstream, calls its ``pause()``, persists the snapshot ref,
        and tears down the local task via ``_close_shared_inplace``.
        The next session-open for the same ``(org, upstream)`` reads
        the persisted ref and resumes from it instead of cold-starting.

        Returns the ``SnapshotRef`` written to persistence, or
        ``None`` when the backend can't pause / no live session
        exists. Backends that can't pause (own-runner pre-F.5,
        local-subprocess) return ``None`` and leave the task alive
        — the caller's idle-policy (kill / cold-restart) keeps
        applying.
        """
        state = self._state.get(upstream_id)
        if state is None or state.shared_task is None:
            return None
        task = state.shared_task
        if not isinstance(task, SandboxConnectionTask):
            return None
        ref = await task.pause()
        if ref is None:
            # Backend can't pause; leave the task alive so the
            # legacy "runner kills on its own timer" path keeps
            # applying. No persistence write happened either.
            return None
        await self._close_shared_inplace(upstream_id)
        return ref

    async def try_discovery_connect(
        self, upstream: UpstreamDefinition
    ) -> None:
        """Try an unauthenticated connection for tool discovery.

        Many MCP servers allow tools/list without auth. If this
        works, tools are discovered at startup. If it fails (server
        requires auth even for listing), we log and skip — tools
        will be discovered after the admin authenticates.
        """
        if upstream.transport != TransportType.streamable_http:
            logger.info(
                "upstream.client.discovery.skipped",
                upstream_id=upstream.id,
                auth_mode=upstream.auth.mode.value,
                transport=upstream.transport.value,
            )
            return
        try:
            await self.connect_shared(upstream)
            logger.info(
                "upstream.client.discovery.success",
                upstream_id=upstream.id,
            )
        except Exception:
            logger.info(
                "upstream.client.discovery.requires_auth",
                upstream_id=upstream.id,
            )

    # ── Admin sessions ──────────────────────────────────────────────

    def has_admin_session(self, upstream_id: str) -> bool:
        state = self._state.get(upstream_id)
        return state is not None and state.admin_session is not None

    def get_admin_session(self, upstream_id: str) -> ClientSession:
        """Return the admin session for an upstream, or raise KeyError."""
        state = self._state.get(upstream_id)
        if state is None or state.admin_session is None:
            raise KeyError(
                f"No active admin session for upstream '{upstream_id}'"
            )
        return state.admin_session

    async def connect_admin_session(
        self,
        upstream: UpstreamDefinition,
        auth: httpx.Auth | None = None,
        bearer_token: str | None = None,
    ) -> None:
        """Create (or replace) the admin session for an upstream."""
        await self._close_admin_inplace(upstream.id)

        session, task = await self._create_task(
            upstream, user_id=ADMIN_USER_ID,
            bearer_token=bearer_token, auth=auth,
        )
        started_config_hash = await self.compute_runtime_hash(upstream)
        self.transition_to_live_admin(
            upstream.id,
            session=session,
            task=task,
            server_info=task.server_info,
            self_description=task.self_description,
            started_config_hash=started_config_hash,
        )
        await self._persist_started_config_hash(
            upstream.id, started_config_hash,
        )
        logger.info(
            "upstream.client.admin_session.created",
            upstream_id=upstream.id,
        )

    # ── Per-user sessions (non-admin) ───────────────────────────────

    def get_session(
        self, upstream_id: str, user_id: str | None = None
    ) -> ClientSession:
        """Get a session for the given upstream.

        Lookup order when ``user_id`` is provided:

          * ``ADMIN_USER_ID`` → admin session.
          * real email       → per-user session dict.

        If none is found under the requested ``user_id``, falls
        through to the shared session. Callers that want the
        admin-first-then-shared pattern (e.g. tool discovery) pass
        ``user_id=ADMIN_USER_ID`` and catch the KeyError.
        """
        state = self._state.get(upstream_id)
        if user_id == ADMIN_USER_ID:
            if state is not None and state.admin_session is not None:
                return state.admin_session
        elif user_id is not None:
            key = (user_id, upstream_id)
            session = self._user_sessions.get(key)
            if session is not None:
                self._user_session_last_used[key] = time.monotonic()
                return session

        if state is not None and state.shared_session is not None:
            return state.shared_session
        raise KeyError(
            f"No active session for upstream '{upstream_id}'"
        )

    def has_user_session(
        self, upstream_id: str, user_id: str
    ) -> bool:
        """True if the given (user_id, upstream_id) pair has a live
        session. Accepts ``ADMIN_USER_ID`` for callers that haven't
        migrated to ``has_admin_session`` yet — routes appropriately."""
        if user_id == ADMIN_USER_ID:
            return self.has_admin_session(upstream_id)
        return (user_id, upstream_id) in self._user_sessions

    async def connect_upstream_for_user(
        self,
        upstream: UpstreamDefinition,
        user_id: str,
        auth: httpx.Auth | None = None,
        bearer_token: str | None = None,
    ) -> None:
        """Create a per-user session (admin or real user).

        For MCP OAuth upstreams, pass ``auth`` (OAuthClientProvider).
        For simple bearer token upstreams, pass ``bearer_token``.

        Routes ``user_id == ADMIN_USER_ID`` to the admin-session
        slot on the upstream-state record, so admin bookkeeping
        (always-on, not swept) stays separate from per-user
        bookkeeping (swept after idle timeout).
        """
        if user_id == ADMIN_USER_ID:
            await self.connect_admin_session(
                upstream, auth=auth, bearer_token=bearer_token,
            )
            return

        key = (user_id, upstream.id)

        if upstream.transport == TransportType.stdio:
            logger.warning(
                "upstream.client.per_user_stdio.subprocess_per_user",
                upstream_id=upstream.id,
                user=user_id,
            )

        # Per-key lock: serialize concurrent connects for the same
        # ``(user, upstream)``. Without this, two callers race their
        # disconnect+create and the first caller's task gets
        # overwritten in the dicts without anyone awaiting its
        # ``close()`` — a silent transport leak.
        lock = self._user_connect_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._user_connect_locks[key] = lock
        async with lock:
            # Tear down existing session if any. Under contention,
            # this observes and closes the prior caller's task —
            # the disconnect path that the lock makes visible.
            await self.disconnect_user_session(upstream.id, user_id)

            session, task = await self._create_task(
                upstream, user_id=user_id,
                bearer_token=bearer_token, auth=auth,
            )
            self._user_sessions[key] = session
            self._user_tasks[key] = task
            self._user_session_last_used[key] = time.monotonic()

            # The per-user session also carries upstream-level metadata
            # — capture it on the state record so dashboard reads
            # benefit (server_info / self_description survive sweeps
            # and dropped per-user sessions).
            if (
                task.server_info is not None
                or task.self_description is not None
            ):
                self._merge_metadata_into_state(
                    upstream.id,
                    server_info=task.server_info,
                    self_description=task.self_description,
                )
            logger.info(
                "upstream.client.user_session.created",
                upstream_id=upstream.id,
                user=user_id,
            )

    def _merge_metadata_into_state(
        self,
        upstream_id: str,
        *,
        server_info: ServerInfo | None,
        self_description: UpstreamSelfDescription | None,
    ) -> None:
        """Update the cached metadata on an upstream's state record
        WITHOUT changing its lifecycle phase.

        Used when a per-user session opens for an upstream that's
        otherwise FAILED / DISABLED at the org level: we still want
        to capture the metadata for diagnostics / future cache-reads,
        but we don't want the per-user activity to silently flip
        the upstream into LIVE / DEFERRED_ATTACH.
        """
        state = self._state.get(upstream_id)
        if state is None:
            self._state[upstream_id] = UpstreamState(
                state=UpstreamConnectionState.FAILED,
                server_info=server_info,
                self_description=self_description,
                last_failure=None,
            )
            return
        if server_info is not None:
            state.server_info = server_info
        if self_description is not None:
            state.self_description = self_description

    async def disconnect_user_session(
        self, upstream_id: str, user_id: str
    ) -> None:
        """Tear down a per-user session. Routes admin to
        ``_close_admin_inplace``; everyone else to the user session
        dict.

        Logs ``Closed per-user session`` at INFO when a non-admin
        session was actually present — the counterpart to ``Created
        per-user session`` emitted on construction, so every
        per-user lifetime is bracketed in prod logs.
        """
        if user_id == ADMIN_USER_ID:
            await self._close_admin_inplace(upstream_id)
            return
        key = (user_id, upstream_id)
        had_session = key in self._user_sessions
        self._user_sessions.pop(key, None)
        self._user_session_last_used.pop(key, None)
        task = self._user_tasks.pop(key, None)
        if task is not None:
            try:
                await task.close()
            except Exception:
                logger.exception(
                    "upstream.client.user_task.close.failed",
                    upstream_id=upstream_id,
                    user=user_id,
                )
        if had_session:
            logger.info(
                "upstream.client.user_session.closed",
                upstream_id=upstream_id,
                user=user_id,
            )

    async def disconnect_all_user_sessions(self, user_id: str) -> int:
        """Tear down all per-user sessions for a given user. Returns count.

        Never touches admin sessions — even if called with
        ``ADMIN_USER_ID`` it returns 0 rather than nuking every
        admin session, because "all sessions for this user" is a
        real-user concept and that sentinel indicates the caller has
        the wrong mental model."""
        if user_id == ADMIN_USER_ID:
            return 0
        keys = [k for k in self._user_sessions if k[0] == user_id]
        for key in keys:
            await self.disconnect_user_session(key[1], user_id)
        return len(keys)

    @property
    def connected_upstream_ids(self) -> list[str]:
        """Upstream IDs with a LIVE session (shared or admin).

        Deferred-attach upstreams are deliberately excluded — this
        accessor backs ``ToolRegistry.refresh_all``, which calls
        ``session.list_tools`` per upstream. A deferred upstream
        has no live session; trying to refresh it would either
        raise (no session) or, worse, eagerly open one and wake
        the paused sandbox we deliberately left paused. Use
        ``ready_upstream_ids`` for the user-facing "is this MCP
        ready?" sense that includes deferred-attach.
        """
        return [
            uid
            for uid, state in self._state.items()
            if state.has_any_session
        ]

    @property
    def ready_upstream_ids(self) -> list[str]:
        """Upstream IDs the user would see as "ready" in the UI.

        Includes:
        - upstreams with a live shared session,
        - upstreams with a live admin session,
        - deferred-attach upstreams (cache populated, lazy reattach
          on first tool call).

        This is what readiness pills, "connected" counts, and
        admin-side org listings should consult — every place that
        answers "is this MCP ready?" from a user's perspective.
        """
        return [
            uid
            for uid, state in self._state.items()
            if state.state in (
                UpstreamConnectionState.LIVE,
                UpstreamConnectionState.DEFERRED_ATTACH,
            )
        ]

    @property
    def all_upstream_ids(self) -> list[str]:
        return list(self._upstreams.keys())

    def register_upstream(self, upstream: UpstreamDefinition) -> None:
        """Register an upstream definition (does not connect)."""
        self._upstreams[upstream.id] = upstream
        if upstream.id not in self._state:
            self._state[upstream.id] = UpstreamState(
                state=UpstreamConnectionState.FAILED,
                last_failure=None,
            )

    async def unregister_upstream(self, upstream_id: str) -> None:
        """Remove an upstream definition and close its sessions."""
        self._upstreams.pop(upstream_id, None)
        await self.transition_to_disabled(
            upstream_id, reason="unregister_upstream",
        )
        # Drop the state record entirely — the upstream no longer
        # exists, so reads should not return a stale DISABLED entry.
        self._state.pop(upstream_id, None)

    async def connect_upstream(
        self,
        upstream: UpstreamDefinition,
        bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> None:
        """Connect to a single upstream and store the shared session."""
        await self.connect_shared(
            upstream, bearer_token=bearer_token, auth=auth
        )

    def is_starting(self, upstream_id: str) -> bool:
        """True iff an admin-clicked Start / Reconnect is in flight.

        Drives the dashboard's "Starting…" disabled-button state
        across tabs and refreshes. Only the cross-tab visible
        admin-initiated reconnect path is reported here — request-
        scoped lazy reattaches (``ensure_shared_connected``) stay
        invisible to the UI by design (the cache satisfies Ready
        before, the live session satisfies Ready after, no flicker
        in between).
        """
        state = self._state.get(upstream_id)
        if state is None:
            return False
        bg = state.background_task
        return bg is not None and not bg.done()

    def register_background_connect_task(
        self, upstream_id: str, task: asyncio.Task[None],
    ) -> None:
        """Hold a strong reference to a fire-and-forget connect task.

        The dashboard reconnect endpoint hands off to a detached
        ``asyncio.Task`` so the user's HTTP request can return while
        the (potentially 30–60s) sandbox cold-pull continues. Without
        a reference here, Python is free to garbage-collect the task
        the moment the request handler returns. Cancels any prior
        in-flight task for the same upstream so a re-click of Start
        doesn't end up racing two warming sandboxes against each
        other.
        """
        self.transition_to_connecting(
            upstream_id, background_task=task,
        )

        def _cleanup(t: asyncio.Task[None]) -> None:
            # Belt-and-suspenders: clear the slot when the task ends
            # so ``is_starting`` returns False even if the connect
            # coroutine forgot to transition. The connect path's
            # own ``connect_shared`` / ``transition_to_failed``
            # normally advances the state ahead of this callback.
            current_state = self._state.get(upstream_id)
            if current_state is None:
                return
            if current_state.background_task is t:
                current_state.background_task = None
                if current_state.state == UpstreamConnectionState.CONNECTING:
                    logger.warning(
                        "upstream.client.background_task.exited_without_transition",
                        upstream_id=upstream_id,
                    )
        task.add_done_callback(_cleanup)

    async def cancel_background_connect_task(self, upstream_id: str) -> None:
        """Cancel a pending fire-and-forget connect, if any.

        Awaits the task to completion (suppressing ``CancelledError``)
        so the caller can safely tear down the session afterwards
        without racing the in-flight ``connect_shared`` that might
        otherwise re-register a session right after we close it.
        """
        state = self._state.get(upstream_id)
        if state is None:
            return
        existing = state.background_task
        if existing is None or existing.done():
            return
        existing.cancel()
        try:
            await existing
        except (asyncio.CancelledError, Exception):
            pass
        # Clear the slot so subsequent ``is_starting`` reads return
        # False. Don't recompute state — the caller's next
        # transition (typically ``transition_to_disabled``) sets
        # the resulting phase.
        current = self._state.get(upstream_id)
        if current is not None and current.background_task is existing:
            current.background_task = None

    async def disconnect_upstream(
        self, upstream_id: str, *, reset_state: bool = True,
    ) -> None:
        """Disconnect every upstream-scoped session (shared + admin).

        Does NOT touch real per-user sessions — those belong to
        individual users and survive an admin-initiated upstream
        disconnect/reconnect. Per-user sessions are reaped by the
        idle sweep or explicit ``disconnect_user_session`` calls.

        ``reset_state`` is retained for call-site compatibility but
        no longer toggles behavior — the registry it used to gate
        was deleted in Phase 5.
        """
        logger.info(
            "upstream.client.disconnect.started",
            upstream_id=upstream_id,
        )
        await self.transition_to_disabled(
            upstream_id, reason="admin_disconnect",
        )
        _ = reset_state

    async def kill_all_for_upstream(self, upstream_id: str) -> int:
        """Close every live session for an upstream — shared, admin,
        AND per-user. Used by the Phase H kill-switch enforcement
        path: when an admin flips a switch, "no sandbox" must mean
        no sandbox, including ones already running for individual
        users.

        Returns the number of sessions closed (for the audit trail).
        """
        closed = 0
        state = self._state.get(upstream_id)
        if state is not None:
            if state.shared_task is not None:
                closed += 1
            if state.admin_task is not None:
                closed += 1
        # Mark the upstream DISABLED — drops shared + admin and
        # cancels any in-flight reconnect.
        await self.transition_to_disabled(
            upstream_id, reason="kill_switch",
        )
        # Per-user: walk both maps, reaping any keyed by this upstream.
        per_user_keys = [
            k for k in self._user_tasks
            if k[1] == upstream_id
        ]
        for key in per_user_keys:
            task = self._user_tasks.pop(key, None)
            self._user_sessions.pop(key, None)
            self._user_session_last_used.pop(key, None)
            if task is not None:
                try:
                    await task.close()
                except Exception:
                    logger.warning(
                        "upstream.client.kill.close_failed",
                        upstream_id=upstream_id,
                        user_id=key[0],
                        exc_info=True,
                    )
                closed += 1
        if closed:
            logger.info(
                "upstream.client.kill.completed",
                upstream_id=upstream_id,
                closed_sessions=closed,
            )
        return closed

    def is_connected(self, upstream_id: str) -> bool:
        """True iff the upstream is reachable for tool calls.

        Backs the UI's "is this MCP reachable?" gate. Two states
        return True:

        - LIVE: a shared and/or admin session is live.
        - DEFERRED_ATTACH: cached metadata satisfies dashboard reads
          while ``ensure_shared_connected`` will reattach lazily on
          the first tool call.

        CONNECTING returns False — admin Reconnect is in flight; the
        UI shows Starting… via ``is_starting``. Per-user sessions
        for ``per_user_oauth`` upstreams don't count here — the
        gate reflects org-level reachability, not per-user sign-in
        state.
        """
        state = self._state.get(upstream_id)
        if state is None:
            return False
        return state.state in (
            UpstreamConnectionState.LIVE,
            UpstreamConnectionState.DEFERRED_ATTACH,
        )

    def get_state(self, upstream_id: str) -> UpstreamState | None:
        """Return the state record for ``upstream_id``, or ``None``.

        Read-only escape hatch — used by tests pinning transition
        side effects and by debug introspection. Production readers
        should prefer the typed accessors (``is_connected``,
        ``ready_upstream_ids``, ``is_starting``, ``has_admin_session``,
        ``get_admin_session``, ``get_session``, ``get_server_info``,
        ``get_self_description``) so the storage shape can evolve
        without churning callers.
        """
        return self._state.get(upstream_id)

    def iter_live_oauth_sessions(
        self, oauth_upstream_ids: set[str],
    ) -> list[tuple[str, str, ClientSession]]:
        """Snapshot every live OAuth session as ``(upstream_id,
        user_id, session)`` triples. Admin sessions surface under
        ``ADMIN_USER_ID``; per-user sessions surface under the real
        user_id.

        Read-only: the caller must not mutate manager state via the
        returned session objects. The ``oauth_upstream_ids`` filter
        lets the §5.5 liveness probe skip service-account upstreams
        — those have no OAuth state to probe, so pinging them would
        be pure noise.

        Returns a snapshot (new list) so iteration is safe against
        concurrent connect/disconnect; callers that want to act on a
        stale entry should go through ``disconnect_user_session`` /
        ``_close_admin_inplace`` with appropriate locking.
        """
        result: list[tuple[str, str, ClientSession]] = []
        for upstream_id, state in self._state.items():
            if upstream_id in oauth_upstream_ids and state.admin_session is not None:
                result.append((upstream_id, ADMIN_USER_ID, state.admin_session))
        for (user_id, upstream_id), session in self._user_sessions.items():
            if upstream_id in oauth_upstream_ids:
                result.append((upstream_id, user_id, session))
        return result

    def any_user_session_for_upstream(
        self, upstream_id: str,
    ) -> ClientSession | None:
        """Return any live per-user session for ``upstream_id``, or None.

        Used by tool-discovery and ``admin_oauth`` resolution when the
        caller does not yet know which user's session to consult.
        Picks a session deterministically (sorted by user_id) so test
        runs are stable; production callers should not rely on the
        exact user the session belongs to. Skips admin sessions on
        purpose — admin_oauth sessions live under their owner's
        email after Phase 2.
        """
        candidates: list[tuple[str, ClientSession]] = []
        for (user_id, uid), session in self._user_sessions.items():
            if uid == upstream_id:
                candidates.append((user_id, session))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def get_upstream(self, upstream_id: str) -> UpstreamDefinition | None:
        return self._upstreams.get(upstream_id)

    def get_server_info(self, upstream_id: str) -> ServerInfo | None:
        state = self._state.get(upstream_id)
        if state is None:
            return None
        return state.server_info

    def get_self_description(
        self, upstream_id: str,
    ) -> UpstreamSelfDescription | None:
        """Return the upstream's captured ``initialize`` self-description.

        Recorded by every connect path (shared / admin / per-user) right
        after a successful ``session.initialize()``. Returns ``None``
        until a connection has succeeded at least once for this upstream.
        """
        state = self._state.get(upstream_id)
        if state is None:
            return None
        return state.self_description

    # --- Per-user session idle sweep ---

    def start_idle_sweep(self) -> None:
        """Start the background task that disconnects idle sessions."""
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(
                self._idle_sweep_loop()
            )

    async def _idle_sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(USER_SESSION_SWEEP_INTERVAL)
                await self._sweep_idle_sessions()
        except asyncio.CancelledError:
            pass

    async def _sweep_idle_sessions(self) -> None:
        # Only iterates user sessions. Admin sessions live in the
        # state record and are exempt from idle cleanup by
        # construction — no string check needed here.
        now = time.monotonic()
        to_disconnect: list[tuple[str, str]] = []
        for (user_id, upstream_id), last_used in (
            self._user_session_last_used.items()
        ):
            if now - last_used > USER_SESSION_IDLE_TIMEOUT:
                to_disconnect.append((user_id, upstream_id))

        for user_id, upstream_id in to_disconnect:
            logger.info(
                "upstream.client.user_session.idle_disconnect",
                upstream_id=upstream_id,
                user=user_id,
            )
            await self.disconnect_user_session(upstream_id, user_id)
