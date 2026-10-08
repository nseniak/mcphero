"""Upstream lifecycle actions, shared by the dashboard and the Admin MCP:
add, import, remove, sign in (Connect), Start, Stop, Remove sign-in,
Refresh tools.

See :mod:`admin_actions` for why both doors call these instead of
carrying their own copies. Every action that changes something runs to
its end once started (``runs_to_completion``): cut half-way, an add left
the new MCP saved as started (it ran at the next boot), a removal left
its sign-ins and DCR client behind for a re-add to inherit, and a Stop
left it saved as stopped while it kept serving.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ValidationError

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.repositories.upstream_config_loader import build_upstream
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
    UpstreamStopped,
)
from mcpolis.adapters.upstream_clients.session_single_flight import (
    ConnectAborted,
)
from mcpolis.domain.model.events import Event
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.model.template_var import is_valid_template_var_name
from mcpolis.domain.model.upstream import (
    AUTH_TOKEN_VARIABLE,
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
    validate_stdio_uses_service_account,
    with_service_account_token,
)
from mcpolis.domain.services.admin_actions import (
    AdminActionDeps,
    AlreadyExists,
    Conflict,
    InvalidRequest,
    NoSignInNeeded,
    NotFound,
    SignInChanged,
    SignInNeedsMembership,
    SignInSlotTaken,
    UnsafeServerUrl,
    UnsupportedSandboxSize,
    log_admin_action,
    publish_policy_changed,
)
from mcpolis.domain.services.audit_actions import (
    REFRESH_TOOLS,
    UPSTREAM_ADDED,
    UPSTREAM_REMOVED,
    record_action,
)
from mcpolis.domain.services.background_tasks import BackgroundTaskSet
from mcpolis.domain.services.cancel_shield import runs_to_completion
from mcpolis.domain.services.org_runtime import OrgRuntime
from mcpolis.domain.services.plan_gates import (
    assert_http_upstream_capacity,
    assert_sandbox_combo_allowed,
    assert_stdio_upstream_capacity,
    resolve_plan,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.sandbox_service import (
    ResourcesUnsupported,
    SandboxResources,
)
from mcpolis.domain.services.system_variables import is_system_variable_name
from mcpolis.domain.services.upstream_connection_service import (
    OAuthConnectResult,
    OAuthFailureReason,
    connect_and_refresh_tools,
    refresh_tools_in_background,
    reopen_stopped_upstream,
    sign_out_of_upstream,
    slot_owner_of,
    start_from_saved_sign_in,
    start_shared_in_background,
    stop_keeping_sign_ins,
)
from mcpolis.domain.services.url_safety import (
    UnsafeUpstreamUrl,
    validate_upstream_url,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Upstream ids become tool-name prefixes (``{upstream}__{tool}``) and
# storage keys, so an imported id must stay within the dashboard
# IdInput charset even when a scripted caller bypasses the UI.
_VALID_UPSTREAM_ID = re.compile(r"[a-z0-9._-]+")
_INVALID_ID_MESSAGE = (
    "Invalid id (allowed: lowercase letters, digits, hyphens, "
    "underscores, dots)"
)


# --- Requests and outcomes ---


class SandboxSizeRequest(BaseModel):
    """Sandbox resources the admin picked. ``None`` keeps the
    ``StdioTransportConfig`` default."""

    cpu_vcpus: float | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    pids_limit: int | None = None
    tmpfs_mb: int | None = None
    persistent_disk_enabled: bool | None = None

    def picks_cpu_or_memory(self) -> bool:
        return self.cpu_vcpus is not None or self.memory_mb is not None


class TemplateVarInput(BaseModel):
    value: str
    is_secret: bool = True


class NewUpstreamRequest(BaseModel):
    """One upstream to add. A ``command`` makes it a hosted stdio MCP,
    otherwise ``url`` makes it a remote HTTP MCP."""

    id: str
    display_name: str
    url: str | None = None
    headers: dict[str, str] = {}
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    sandbox: SandboxSizeRequest = SandboxSizeRequest()
    auth_mode: str = "service_account"
    auth_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = []
    template_vars: dict[str, TemplateVarInput] = {}


class ImportRow(BaseModel):
    """One server picked in the import dialog. ``config`` is ``None``
    when the uploaded file has no server under ``original_id``."""

    original_id: str
    target_id: str
    config: dict[str, Any] | None


class ImportRowError(BaseModel):
    id: str
    error: str


class ImportOutcome(BaseModel):
    added: list[str]
    errors: list[ImportRowError]


@dataclass(frozen=True)
class StartResult:
    """How a service-account Start ended. ``started`` False with no
    ``error`` means a Stop, or a newer Start, interrupted it.
    ``discovery_error`` is set when it started but its tool discovery
    failed."""

    started: bool
    error: str | None = None
    discovery_error: str | None = None


@dataclass(frozen=True)
class StartOutcome:
    """A Start either signs the admin in (OAuth modes), connects the
    shared session in the background (service_account), or, when asked
    not to restart, finds the upstream ``already`` running or starting."""

    sign_in: OAuthConnectResult | None = None
    # Resolves when the background Start ends; cancelled if a Stop or
    # a second Start cancels it.
    started: asyncio.Future[StartResult] | None = None
    already: Literal["running", "starting"] | None = None


# --- Checks shared by add, import and edit ---


def assert_upstream_capacity(
    plan: PlanName,
    existing: Iterable[UpstreamDefinition],
    new_transports: Iterable[TransportType],
    *,
    source: str,
    org_id: str,
    actor_email: str,
) -> None:
    """Raise ``PlanLimitExceeded`` when adding upstreams of
    ``new_transports`` would cross the plan's per-transport caps."""
    running = Counter(u.transport for u in existing)
    for transport in new_transports:
        if transport == TransportType.stdio:
            assert_stdio_upstream_capacity(
                plan, running[transport],
                source=source, org_id=org_id, actor_email=actor_email,
            )
        else:
            assert_http_upstream_capacity(
                plan, running[transport],
                source=source, org_id=org_id, actor_email=actor_email,
            )
        running[transport] += 1


async def check_sandbox_size(
    client_manager: UpstreamClientManager,
    stdio: StdioTransportConfig,
    *,
    plan: PlanName | None,
    source: str,
    org_id: str,
    actor_email: str,
) -> None:
    """Refuse a sandbox size the provider cannot run, then (when
    ``plan`` is given) one the plan does not allow.

    The provider check runs first so an off-grid value gets the
    specific field hint. Callers pass ``plan`` only when the admin
    picked CPU or RAM, so a create that leaves the size alone keeps the
    model default without a plan check.
    """
    try:
        await client_manager.validate_sandbox_resources(
            SandboxResources(
                cpu_vcpus=stdio.cpu_vcpus,
                memory_mb=stdio.memory_mb,
                disk_gb=stdio.disk_gb,
                pids_limit=stdio.pids_limit,
            ),
        )
    except ResourcesUnsupported as exc:
        raise UnsupportedSandboxSize(
            str(exc), field=exc.field, value=exc.value,
        ) from None
    if plan is not None:
        assert_sandbox_combo_allowed(
            plan, stdio.cpu_vcpus, stdio.memory_mb,
            source=source, org_id=org_id, actor_email=actor_email,
        )


def check_upstream_id(upstream_id: str) -> None:
    """Upstream ids become tool-name prefixes (``{upstream}__{tool}``) and
    storage keys, so they must stay within the dashboard form's charset
    whoever calls: a script, or the Admin MCP's AI assistant."""
    if not _VALID_UPSTREAM_ID.fullmatch(upstream_id):
        raise InvalidRequest(_INVALID_ID_MESSAGE)


def check_upstream_url(url: str) -> None:
    try:
        validate_upstream_url(url)
    except UnsafeUpstreamUrl as exc:
        raise UnsafeServerUrl(exc.reason) from None


def _save_token_as_variable(
    upstream: UpstreamDefinition,
    auth_token: str | None,
    template_vars: dict[str, TemplateVarInput],
) -> tuple[UpstreamDefinition, dict[str, TemplateVarInput]]:
    """Turn an ``auth_token`` into the secret Variable ``MCP_AUTH_TOKEN``
    plus a reference to it (see ``with_service_account_token``)."""
    try:
        upstream, token = with_service_account_token(upstream, auth_token)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from None
    if token is None:
        return upstream, template_vars
    if AUTH_TOKEN_VARIABLE in template_vars:
        raise InvalidRequest(
            f"auth_token is saved as the Variable {AUTH_TOKEN_VARIABLE}, "
            "and a Variable of that name was sent too: send only one"
        )
    return upstream, {
        **template_vars,
        AUTH_TOKEN_VARIABLE: TemplateVarInput(value=token, is_secret=True),
    }


def check_template_var_names(names: Iterable[str]) -> None:
    for name in names:
        if not is_valid_template_var_name(name):
            raise InvalidRequest(
                f"Invalid variable name {name!r}: must match "
                "[A-Z_][A-Z0-9_]*",
            )
        if is_system_variable_name(name):
            raise InvalidRequest(
                f"Cannot create a Variable named {name!r}: that name is "
                f"reserved for the read-only system variable ${{{name}}}",
            )


async def admin_oauth_owner(
    connection_store: ConnectionStore,
    org_id: str,
    upstream_id: str,
    excluding_email: str | None,
    *,
    policy_engine: PolicyEngine,
) -> str | None:
    """Return the email of the admin who holds the upstream's single
    admin sign-in slot, unless that is ``excluding_email``; ``None`` when
    the slot is free or is the caller's own (so their re-connect of an
    upstream they already hold succeeds instead of conflicting).

    The holder is ``slot_owner_of``'s: the admin the admin tab shows,
    whose sign-in Remove sign-in deletes and tool calls use. The slot is
    uniform across both OAuth modes: per_user_oauth storage allows
    several admin rows, and one rule picks the same one everywhere, so a
    refusal never names an admin the dashboard doesn't show.
    """
    owner = await slot_owner_of(
        connection_store, org_id, upstream_id,
        admin_emails=policy_engine.get_admin_emails(),
    )
    if owner is None or owner == excluding_email:
        return None
    return owner


async def resolve_upstream_readiness(
    upstream: UpstreamDefinition,
    org_id: str,
    connection_store: ConnectionStore | None,
    runtime: OrgRuntime,
) -> tuple[bool, str | None]:
    """Compute ``(ready, slot_owner)`` for an upstream.

    The single source of truth for the admin tab, /my-tools and Refresh
    tools. Definitions:

    - ``service_account``: Ready iff the shared session is live;
      ``slot_owner`` is always ``None``.
    - ``admin_oauth`` and ``per_user_oauth``: Ready iff at least one
      admin has a stored token row. ``slot_owner`` is that admin's
      email, picked by ``slot_owner_of``.

    Non-admin users' rows do NOT count towards readiness. The
    invariant "Ready ⇔ admin authenticated" is what the admin tab
    UI relies on, and it's load-bearing for non-admin /my-tools
    flows: if no admin is signed in, the upstream is Not Ready
    even when non-admins have personal rows.

    A stopped OAuth upstream is Not Ready whatever its sign-ins: Stop
    keeps them, and every call is refused until Start. Its
    ``slot_owner`` is still the admin whose kept sign-in Start reuses
    (``None`` when none was kept), so the admin tab can name it.
    """
    if upstream.auth.mode == AuthMode.service_account:
        return runtime.client_manager.is_connected(upstream.id), None
    if connection_store is None:
        return False, None
    owner = await slot_owner_of(
        connection_store, org_id, upstream.id,
        admin_emails=runtime.policy_engine.get_admin_emails(),
    )
    if runtime.client_manager.is_stopped(upstream.id):
        return False, owner
    return owner is not None, owner


# --- The actions ---


class UpstreamAdminService:
    """Upstream lifecycle actions for one org admin at a time."""

    def __init__(self, deps: AdminActionDeps) -> None:
        self._deps = deps
        # Jobs left running after an action returns (the fingerprint
        # save once a sign-in lands), held until each ends.
        self._background_tasks = BackgroundTaskSet()

    async def _upstream(
        self, runtime: OrgRuntime, org_id: str, upstream_id: str,
    ) -> UpstreamDefinition:
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise NotFound(f"Upstream '{upstream_id}' not found")
        return upstream

    def _publish(self, org_id: str, event_type: str, **payload: object) -> None:
        if self._deps.event_bus is not None:
            self._deps.event_bus.publish(
                org_id, Event(type=event_type, payload=payload),
            )

    # --- Add / import / remove ---

    async def _persist_new_upstream(
        self,
        runtime: OrgRuntime,
        org_id: str,
        upstream: UpstreamDefinition,
        template_vars: dict[str, TemplateVarInput],
    ) -> None:
        """Save a checked upstream, its Variables and its per-role access
        entries, stopped until an admin Starts it. All or nothing: when a
        step after the save fails, the upstream is removed again."""
        repo = self._deps.template_var_repo
        if template_vars and repo is None:
            raise RuntimeError("Variables need a template_var_repo")
        try:
            await runtime.config_service.add_upstream(org_id, upstream)
        except ValueError as e:
            raise AlreadyExists(str(e)) from None
        try:
            # Stopped first, in storage (so a restart does not connect
            # it) and in memory (so a tool call does not start it).
            # ``set_disabled`` writes an explicit ``enabled: False``;
            # clearing the marker instead would fall back to
            # default-enabled.
            if self._deps.connection_store is not None:
                await self._deps.connection_store.set_disabled(
                    org_id, upstream.id,
                )
            await runtime.client_manager.transition_to_disabled(
                upstream.id, reason="added_stopped",
            )
            if repo is not None:
                for name, spec in template_vars.items():
                    await repo.set(
                        org_id, upstream.id, name, spec.value,
                        is_secret=spec.is_secret,
                    )
            await runtime.config_service.grant_role_access(
                org_id, upstream.id,
            )
        except Exception:
            await self._remove_half_added(runtime, org_id, upstream.id)
            raise

    async def _remove_half_added(
        self, runtime: OrgRuntime, org_id: str, upstream_id: str,
    ) -> None:
        try:
            await runtime.config_service.remove_upstream(org_id, upstream_id)
        except Exception:
            logger.exception(
                "upstream.admin.add.rollback_failed",
                upstream_id=upstream_id, org_id=org_id,
            )

    async def _audit_added(
        self, org_id: str, upstream: UpstreamDefinition, *, actor: str,
    ) -> None:
        await record_action(
            self._deps.audit_repo, org_id,
            action=UPSTREAM_ADDED, actor=actor, upstream_id=upstream.id,
        )

    @runs_to_completion
    async def add_upstream(
        self,
        org_id: str,
        request: NewUpstreamRequest,
        *,
        actor: str,
        source: str,
    ) -> UpstreamDefinition:
        """Check, save and stop one new upstream. ``source`` names the
        door for the plan-limit analytics event."""
        runtime = await self._deps.runtime_manager.get(org_id)
        check_upstream_id(request.id)
        if not request.url and not request.command:
            raise InvalidRequest("Either 'url' or 'command' is required")
        if request.command and not self._deps.allow_stdio_mcp:
            raise InvalidRequest("Stdio MCP servers are disabled")
        transport = (
            TransportType.stdio if request.command
            else TransportType.streamable_http
        )
        plan = await resolve_plan(self._deps.org_repo, org_id)
        assert_upstream_capacity(
            plan, await runtime.config_service.list_upstreams(org_id),
            [transport],
            source=source, org_id=org_id, actor_email=actor,
        )
        try:
            mode = AuthMode(request.auth_mode)
            validate_stdio_uses_service_account(transport, mode)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        check_template_var_names(request.template_vars)
        auth = UpstreamAuthConfig(
            mode=mode,
            client_id=request.client_id,
            client_secret=request.client_secret,
            scopes=request.scopes,
        )
        try:
            if request.command:
                stdio = StdioTransportConfig(
                    command=request.command,
                    args=request.args,
                    env=request.env,
                    **request.sandbox.model_dump(exclude_none=True),
                )
                await check_sandbox_size(
                    runtime.client_manager, stdio,
                    plan=plan if request.sandbox.picks_cpu_or_memory() else None,
                    source=source, org_id=org_id, actor_email=actor,
                )
                upstream = UpstreamDefinition(
                    id=request.id,
                    display_name=request.display_name,
                    transport=transport,
                    stdio=stdio,
                    auth=auth,
                )
            else:
                assert request.url is not None
                check_upstream_url(request.url)
                upstream = UpstreamDefinition(
                    id=request.id,
                    display_name=request.display_name,
                    transport=transport,
                    http=HttpTransportConfig(
                        url=request.url, headers=request.headers,
                    ),
                    auth=auth,
                )
        except ValidationError as exc:
            raise InvalidRequest(str(exc)) from None
        upstream, template_vars = _save_token_as_variable(
            upstream, request.auth_token, request.template_vars,
        )
        await self._persist_new_upstream(
            runtime, org_id, upstream, template_vars,
        )
        await self._audit_added(org_id, upstream, actor=actor)
        get_analytics().track_async(
            actor,
            "upstream_added",
            {
                "upstream_id": upstream.id,
                "transport": upstream.transport.value,
                "auth_mode": upstream.auth.mode.value,
            },
        )
        return upstream

    @runs_to_completion
    async def import_upstreams(
        self, org_id: str, rows: list[ImportRow], *, actor: str,
    ) -> ImportOutcome:
        """Add every importable row; report the others as row errors.

        Three passes. Pass 1 checks each row without writing anything,
        so the plan gates count only what will actually be created.
        Pass 2 applies the plan gates to the whole set: a plan refusal
        stops the whole import, because one upgrade prompt beats a
        partial success. Pass 3 saves.
        """
        source = "dashboard.import_confirm"
        runtime = await self._deps.runtime_manager.get(org_id)
        existing_upstreams = await runtime.config_service.list_upstreams(org_id)
        existing_ids = {u.id for u in existing_upstreams}
        seen_ids: set[str] = set()
        errors: list[ImportRowError] = []
        checked: list[tuple[UpstreamDefinition, bool]] = []
        for row in rows:
            tid = row.target_id
            if not tid or not _VALID_UPSTREAM_ID.fullmatch(tid):
                errors.append(ImportRowError(
                    id=tid or row.original_id, error=_INVALID_ID_MESSAGE,
                ))
                continue
            if tid in existing_ids:
                errors.append(ImportRowError(
                    id=tid, error="An upstream with this id already exists",
                ))
                continue
            if tid in seen_ids:
                errors.append(ImportRowError(
                    id=tid, error="Duplicate id in this import",
                ))
                continue
            config = row.config
            if config is None:
                errors.append(ImportRowError(
                    id=tid,
                    error="Source server not found in the uploaded config",
                ))
                continue
            if "command" in config and not self._deps.allow_stdio_mcp:
                errors.append(ImportRowError(
                    id=tid, error="Stdio MCP servers are disabled",
                ))
                continue
            seen_ids.add(tid)
            try:
                upstream = build_upstream(tid, config, {})
                if upstream.http is not None:
                    check_upstream_url(upstream.http.url)
                if upstream.stdio is not None:
                    await check_sandbox_size(
                        runtime.client_manager, upstream.stdio,
                        plan=None,
                        source=source, org_id=org_id, actor_email=actor,
                    )
            except UnsafeServerUrl as exc:
                errors.append(ImportRowError(
                    id=tid, error=f"UNSAFE_UPSTREAM_URL: {exc.reason}",
                ))
                continue
            except Exception as exc:
                errors.append(ImportRowError(id=tid, error=str(exc)))
                continue
            picks_size = "cpu_vcpus" in config or "memory_mb" in config
            checked.append((upstream, picks_size))

        plan = await resolve_plan(self._deps.org_repo, org_id)
        assert_upstream_capacity(
            plan, existing_upstreams,
            [upstream.transport for upstream, _ in checked],
            source=source, org_id=org_id, actor_email=actor,
        )
        for upstream, picks_size in checked:
            if upstream.stdio is not None and picks_size:
                assert_sandbox_combo_allowed(
                    plan, upstream.stdio.cpu_vcpus, upstream.stdio.memory_mb,
                    source=source, org_id=org_id, actor_email=actor,
                )

        added: list[str] = []
        for upstream, _ in checked:
            try:
                await self._persist_new_upstream(runtime, org_id, upstream, {})
            except Exception as exc:
                errors.append(ImportRowError(id=upstream.id, error=str(exc)))
                continue
            added.append(upstream.id)
            await self._audit_added(org_id, upstream, actor=actor)
        return ImportOutcome(added=added, errors=errors)

    @runs_to_completion
    async def remove_upstream(
        self, org_id: str, upstream_id: str, *, actor: str,
    ) -> None:
        """Remove an upstream and everything saved for it: its sign-ins,
        DCR client, Variables, Sandbox files and sandbox.

        Runs one at a time with a Stop or Start of the same upstream
        (``stop_start_lock``). A Start that read the upstream before the
        removal then finds it removed and stops; one already launched is
        cancelled by the removal. Without the lock, a removal landing in
        the middle of a Start brought the removed MCP back, live, with a
        new sandbox and its tools, and nothing could stop it afterwards.
        """
        runtime = await self._deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        try:
            async with runtime.client_manager.stop_start_lock(upstream_id):
                await runtime.config_service.remove_upstream(org_id, upstream_id)
        except ValueError as e:
            raise NotFound(str(e)) from None
        # Provider-side teardown (E2B Volumes, persistence refs). Runs
        # after config removal so a partial failure here can't leave
        # the upstream half-deleted; every backend's teardown is
        # idempotent, so a later reconciliation can still clean up.
        try:
            await runtime.client_manager.cleanup_sandbox_state_for_upstream(
                upstream_id,
            )
        except Exception:
            logger.warning(
                "upstream.remove.sandbox_cleanup_failed",
                org_id=org_id, upstream_id=upstream_id, exc_info=True,
            )
        if self._deps.connection_store is not None:
            await self._deps.connection_store.clear_connection_error(
                org_id, upstream_id,
            )
            # Remove any stopped marker so a fresh re-add starts clean.
            await self._deps.connection_store.set_enabled(org_id, upstream_id)
        # The upstream's tools are gone; push so clients stop listing them.
        publish_policy_changed(self._deps.event_bus, org_id)
        await record_action(
            self._deps.audit_repo, org_id,
            action=UPSTREAM_REMOVED, actor=actor, upstream_id=upstream_id,
        )
        get_analytics().track_async(
            actor,
            "upstream_removed",
            {
                "upstream_id": upstream_id,
                "transport": upstream.transport.value if upstream else "unknown",
                "auth_mode": upstream.auth.mode.value if upstream else "unknown",
            },
        )

    # --- Sign in, Start, Stop ---

    async def _snapshot_started_config_hash(
        self, runtime: OrgRuntime, org_id: str, upstream: UpstreamDefinition,
    ) -> None:
        """Record the saved config the running upstream started from.

        OAuth upstreams compute ``ready`` from token existence alone, so
        without this snapshot the dirty-config banner could never fire
        for them: the next config edit compares against this hash.
        """
        if self._deps.connection_store is None:
            return
        hash_value = await runtime.client_manager.compute_runtime_hash(upstream)
        try:
            await self._deps.connection_store.set_started_config_hash(
                org_id, upstream.id, hash_value,
            )
        except Exception:
            logger.warning(
                "upstream.admin.snapshot_hash_failed",
                upstream_id=upstream.id, org_id=org_id, exc_info=True,
            )

    async def _sign_in(
        self,
        runtime: OrgRuntime,
        org_id: str,
        upstream: UpstreamDefinition,
        *,
        actor: str,
        action: Literal["connect", "reconnect"],
        on_tools_discovered: Callable[[str | None], None] | None,
    ) -> OAuthConnectResult:
        """Sign ``actor`` in to an OAuth upstream: the shared part of
        Connect and of Start on an OAuth upstream.

        Both OAuth modes key tokens by the signing-in admin's email. For
        admin_oauth the gateway serves everyone from any admin's valid
        token; per_user_oauth looks tokens up by caller.

        ``on_tools_discovered`` hears the outcome of the tool discovery a
        live sign-in starts: the error text, or ``None`` on success.
        """
        store = self._deps.connection_store
        coordinator = self._deps.auth_coordinator
        if store is None or coordinator is None:
            raise InvalidRequest("OAuth is not configured")
        event_bus = self._deps.event_bus

        def _tokens_acquired() -> None:
            # Slow path: tokens arrive via the OAuth callback after the
            # request returned. Tell the waiting dashboard tab, and every
            # gateway session, that the upstream now works.
            self._publish(
                org_id, "upstream_tokens_acquired", upstream_id=upstream.id,
            )
            publish_policy_changed(event_bus, org_id)
            self._background_tasks.spawn(
                self._snapshot_started_config_hash(runtime, org_id, upstream),
            )

        def _sign_in_failed(error: str, reason: OAuthFailureReason) -> None:
            del reason  # admin sign-ins emit no product analytics
            self._publish(
                org_id, "upstream_oauth_error",
                upstream_id=upstream.id, error=error,
            )

        def _tools_refreshed() -> None:
            # Flips the dashboard's "Fetching info" pill off and pulls
            # in the real tool count.
            publish_policy_changed(event_bus, org_id)

        async def _slot_taken_meanwhile() -> str | None:
            refusal = await self._slot_refusal(
                runtime, org_id, upstream.id, actor,
            )
            return str(refusal) if refusal is not None else None

        # Only an admin_oauth upstream has one admin sign-in, which serves
        # every member: another admin who took it while ``actor`` was on
        # the consent page wins. On a per_user_oauth upstream each admin's
        # sign-in is their own, and lands whoever else signed in meanwhile.
        sign_in_check = (
            _slot_taken_meanwhile
            if upstream.auth.mode == AuthMode.admin_oauth
            else None
        )
        result = await connect_and_refresh_tools(
            org_id=org_id,
            upstream=upstream,
            effective_user=actor,
            connection_store=store,
            auth_coordinator=coordinator,
            client_manager=runtime.client_manager,
            tool_registry=runtime.tool_registry,
            server_url=self._deps.server_url,
            on_tokens_acquired=_tokens_acquired,
            # Only Connect reports a failed sign-in to the waiting tab, as
            # before. An abandoned earlier sign-in still fails later and
            # would surface as the newer one's failure (see STATUS.md).
            on_error=_sign_in_failed if action == "connect" else None,
            on_tools_refreshed=_tools_refreshed,
            on_discovery_done=on_tools_discovered,
            sign_in_check=sign_in_check,
        )
        return await self._record_sign_in(
            runtime, org_id, upstream, result, actor=actor, action=action,
        )

    async def _record_sign_in(
        self,
        runtime: OrgRuntime,
        org_id: str,
        upstream: UpstreamDefinition,
        result: OAuthConnectResult,
        *,
        actor: str,
        action: Literal["connect", "reconnect"],
    ) -> OAuthConnectResult:
        """Record how a sign-in, or a Start from a kept sign-in, ended.

        A removal that landed meanwhile owns the outcome, like a Stop:
        nothing is saved for the removed upstream (a later add under the
        same id would inherit it) and the row says ``aborted``."""
        store = self._deps.connection_store
        assert store is not None  # both callers checked it
        log_fields: dict[str, object] = {
            "upstream_id": upstream.id, "org_id": org_id, "action": action,
        }
        if runtime.client_manager.is_removed(upstream.id):
            result = OAuthConnectResult(aborted=True)
        if result.connected:
            await store.clear_connection_error(org_id, upstream.id)
            await self._snapshot_started_config_hash(runtime, org_id, upstream)
            publish_policy_changed(self._deps.event_bus, org_id)
            logger.info("upstream.admin.sign_in.success", **log_fields)
        elif result.authorization_url:
            logger.info("upstream.admin.sign_in.pending", **log_fields)
        elif result.aborted:
            # A Stop landed meanwhile: it owns the state, nothing to show.
            logger.info("upstream.admin.sign_in.aborted", **log_fields)
        elif result.error:
            await store.set_connection_error(org_id, upstream.id, result.error)
            logger.warning(
                "upstream.admin.sign_in.failed",
                error=result.error, **log_fields,
            )
        await log_admin_action(
            self._deps.audit_repo, org_id,
            action=action,
            upstream_id=upstream.id,
            admin_email=actor,
            outcome=(
                "success" if result.connected
                else "pending" if result.authorization_url
                else "aborted" if result.aborted
                else "error"
            ),
            error_message=result.error,
        )
        return result

    async def _kept_sign_in(
        self, runtime: OrgRuntime, org_id: str, upstream_id: str,
    ) -> str | None:
        """The admin whose sign-in a Stop kept, when the upstream is
        stopped: Start reconnects from it, whoever clicks, with no
        sign-in page and no "already signed in" refusal."""
        store = self._deps.connection_store
        if store is None or not runtime.client_manager.is_stopped(upstream_id):
            return None
        return await slot_owner_of(
            store, org_id, upstream_id,
            admin_emails=runtime.policy_engine.get_admin_emails(),
        )

    async def _start_from_kept_sign_in(
        self,
        runtime: OrgRuntime,
        org_id: str,
        upstream: UpstreamDefinition,
        owner: str,
        *,
        actor: str,
        action: Literal["connect", "reconnect"],
        on_tools_discovered: Callable[[str | None], None] | None,
    ) -> OAuthConnectResult:
        """Start a stopped OAuth upstream from the admin sign-in Stop
        kept (``owner``'s, whoever ``actor`` is)."""
        store = self._deps.connection_store
        assert store is not None  # ``_kept_sign_in`` found a sign-in in it
        event_bus = self._deps.event_bus
        logger.info(
            "upstream.admin.start.from_kept_sign_in",
            upstream_id=upstream.id, org_id=org_id, owner=owner,
            admin_email=actor,
        )
        try:
            result = await start_from_saved_sign_in(
                org_id=org_id,
                upstream=upstream,
                owner=owner,
                connection_store=store,
                client_manager=runtime.client_manager,
                tool_registry=runtime.tool_registry,
                server_url=self._deps.server_url,
                on_tools_refreshed=lambda: publish_policy_changed(event_bus, org_id),
                on_discovery_done=on_tools_discovered,
            )
        except UpstreamStopped:
            # Removed before its Stop could be lifted.
            raise NotFound(f"Upstream '{upstream.id}' not found") from None
        return await self._record_sign_in(
            runtime, org_id, upstream, result, actor=actor, action=action,
        )

    async def _refuse_sign_in(
        self, runtime: OrgRuntime, org_id: str, upstream_id: str, actor: str,
    ) -> None:
        """Refuse a sign-in while OAuth is not configured, for someone who
        is not a member of the org (an MCP Hero operator browsing it: the
        upstream's callback would refuse their sign-in at the end), or
        while another admin holds the upstream's single admin sign-in
        slot: they must be disconnected first."""
        if self._deps.connection_store is None or self._deps.auth_coordinator is None:
            raise InvalidRequest("OAuth is not configured")
        if not runtime.policy_engine.is_member(actor):
            raise SignInNeedsMembership()
        refusal = await self._slot_refusal(runtime, org_id, upstream_id, actor)
        if refusal is not None:
            raise refusal

    async def _slot_refusal(
        self, runtime: OrgRuntime, org_id: str, upstream_id: str, actor: str,
    ) -> SignInSlotTaken | None:
        """The refusal while another admin holds the upstream's admin
        sign-in slot, else ``None``. Asked when ``actor`` clicks Connect,
        then, on an admin_oauth upstream, again at the upstream's callback
        and right before the new sign-in is saved
        (``initiate_oauth_connection``'s ``sign_in_check``): another admin
        may sign in while ``actor`` is on the consent page."""
        store = self._deps.connection_store
        if store is None:
            return None
        owner = await admin_oauth_owner(
            store, org_id, upstream_id, actor,
            policy_engine=runtime.policy_engine,
        )
        return SignInSlotTaken(owner) if owner is not None else None

    @runs_to_completion
    async def connect_upstream(
        self,
        org_id: str,
        upstream_id: str,
        *,
        actor: str,
        on_tools_discovered: Callable[[str | None], None] | None = None,
    ) -> OAuthConnectResult:
        """Sign the admin in to an OAuth upstream (the dashboard's
        Connect)."""
        runtime = await self._deps.runtime_manager.get(org_id)
        if (
            self._deps.connection_store is None
            or self._deps.auth_coordinator is None
        ):
            raise InvalidRequest("OAuth is not configured")
        upstream = await self._upstream(runtime, org_id, upstream_id)
        if upstream.auth.mode == AuthMode.service_account:
            raise NoSignInNeeded()
        return await self._connect_oauth(
            runtime, org_id, upstream,
            actor=actor, action="connect",
            on_tools_discovered=on_tools_discovered,
        )

    async def _connect_oauth(
        self,
        runtime: OrgRuntime,
        org_id: str,
        upstream: UpstreamDefinition,
        *,
        actor: str,
        action: Literal["connect", "reconnect"],
        on_tools_discovered: Callable[[str | None], None] | None,
    ) -> OAuthConnectResult:
        """Connect and Start of an OAuth upstream, the same for both: from
        the admin sign-in a Stop kept, else sign ``actor`` in (refused
        while another admin holds the sign-in slot).

        Never drops a session. A Start used to disconnect first, which
        since Stop closes every member's session meant that the Admin
        MCP's ``start_upstream`` (annotated non-destructive) on a working
        OAuth server cut off every member."""
        upstream_id = upstream.id
        kept_owner = await self._kept_sign_in(runtime, org_id, upstream_id)
        if kept_owner is not None:
            return await self._start_from_kept_sign_in(
                runtime, org_id, upstream, kept_owner,
                actor=actor, action=action,
                on_tools_discovered=on_tools_discovered,
            )
        await self._refuse_sign_in(runtime, org_id, upstream_id, actor)
        logger.info(
            "upstream.admin.connect.requested",
            upstream_id=upstream_id, admin_email=actor, org_id=org_id,
            action=action,
        )
        # Lift any Stop, saved and in this app, so the upstream counts as
        # started and the admin's session may open.
        try:
            await reopen_stopped_upstream(
                org_id=org_id,
                upstream_id=upstream_id,
                client_manager=runtime.client_manager,
                connection_store=self._deps.connection_store,
            )
        except UpstreamStopped:
            raise NotFound(f"Upstream '{upstream_id}' not found") from None
        return await self._sign_in(
            runtime, org_id, upstream,
            actor=actor, action=action,
            on_tools_discovered=on_tools_discovered,
        )

    @runs_to_completion
    async def start_upstream(
        self,
        org_id: str,
        upstream_id: str,
        *,
        actor: str,
        restart: bool = True,
        on_tools_discovered: Callable[[str | None], None] | None = None,
    ) -> StartOutcome:
        """The dashboard's Start.

        An OAuth upstream is connected the way Connect does it
        (``_connect_oauth``): no session is dropped. A service-account one
        drops its running session and connects again; with ``restart``
        False, one already running or starting is left alone instead (the
        Admin MCP: its Start must not cut off in-flight calls or
        cold-start a sandbox again)."""
        runtime = await self._deps.runtime_manager.get(org_id)
        upstream = await self._upstream(runtime, org_id, upstream_id)
        logger.info(
            "upstream.admin.start.requested",
            upstream_id=upstream_id,
            auth_mode=upstream.auth.mode.value,
            admin_email=actor,
            org_id=org_id,
        )
        if upstream.auth.mode != AuthMode.service_account:
            return StartOutcome(sign_in=await self._connect_oauth(
                runtime, org_id, upstream,
                actor=actor, action="reconnect",
                on_tools_discovered=on_tools_discovered,
            ))
        # The checks, the teardown and the launch all run under the
        # Stop/Start lock: a second Start then sees this one starting
        # (two Admin MCP Starts used to cut each other), and a removal or
        # a Stop lands before the whole Start or after it.
        async with runtime.client_manager.stop_start_lock(upstream_id):
            return await self._start_shared(
                runtime, org_id, upstream_id,
                actor=actor, restart=restart,
            )

    async def _start_shared(
        self,
        runtime: OrgRuntime,
        org_id: str,
        upstream_id: str,
        *,
        actor: str,
        restart: bool,
    ) -> StartOutcome:
        """The service-account Start, under the Stop/Start lock."""
        manager = runtime.client_manager
        # Read again under the lock: removed meanwhile (the removal holds
        # the lock too), there is nothing to start.
        if manager.is_removed(upstream_id):
            raise NotFound(f"Upstream '{upstream_id}' not found")
        upstream = await self._upstream(runtime, org_id, upstream_id)
        if not restart:
            if manager.is_starting(upstream_id):
                return StartOutcome(already="starting")
            if manager.is_connected(upstream_id):
                return StartOutcome(already="running")
        # A failed disconnect must not block the Start.
        # ``reset_state=False`` skips the synthetic ``cold`` registry
        # transition: the Start marks the upstream warming right away,
        # and the gap between the two would flicker the button back to
        # "Start" mid-cold-pull.
        try:
            await manager.disconnect_upstream(upstream_id, reset_state=False)
        except Exception:
            logger.warning(
                "upstream.admin.start.disconnect_failed",
                upstream_id=upstream_id, org_id=org_id, exc_info=True,
            )
        store = self._deps.connection_store
        if store is not None:
            # Reset the previous attempt's error banner so the dashboard
            # shows a clean slate while the Start runs.
            await store.clear_connection_error(org_id, upstream_id)
        manager.log_buffers.get_or_create(upstream_id).clear()

        # Fire-and-forget: a hosted stdio MCP's cold start (package
        # download, then MCP ``initialize``) can take 30s or more. The
        # dashboard follows progress on the ``sandbox_state_changed``
        # stream and on the recorded connection error, both of which
        # survive a tab switch or reload.
        started: asyncio.Future[StartResult] = (
            asyncio.get_running_loop().create_future()
        )

        async def _start_in_background() -> None:
            started.set_result(
                await self._connect_shared(runtime, org_id, upstream, actor),
            )

        def _settle(task: asyncio.Task[None]) -> None:
            # Resolve ``started`` however the task ended, including a
            # cancel that lands before its first step.
            if started.done():
                return
            if task.cancelled():
                started.cancel()
                return
            exc = task.exception()
            logger.error(
                "upstream.admin.start.crashed",
                upstream_id=upstream_id, org_id=org_id, exc_info=exc,
            )
            started.set_result(StartResult(
                started=False,
                error=runtime.client_manager.hide_secrets_in_error(
                    upstream_id, str(exc) or exc.__class__.__name__,
                ) if exc else None,
            ))

        # Saved as started (the boot reconciler picks the upstream up on
        # the next restart), then launched.
        try:
            task = await start_shared_in_background(
                org_id=org_id,
                upstream_id=upstream_id,
                client_manager=manager,
                connection_store=store,
                connect=_start_in_background,
            )
        except UpstreamStopped:
            raise NotFound(f"Upstream '{upstream_id}' not found") from None
        task.add_done_callback(_settle)
        logger.info(
            "upstream.admin.start.scheduled",
            upstream_id=upstream_id, org_id=org_id,
        )
        return StartOutcome(started=started)

    @staticmethod
    def _stopped_meanwhile(
        runtime: OrgRuntime, upstream_id: str, stops_seen: int,
    ) -> bool:
        """Did a Stop or a removal land since the Start's connect began
        (``stops_seen``, read then)? Once that connect is live, a Stop no
        longer cancels the Start. Asking whether the upstream is stopped
        NOW is not enough: a second Start may already have lifted the
        Stop, and this Start would then report and audit a success for
        the session the Stop closed."""
        return runtime.client_manager.stopped_since(upstream_id, stops_seen)

    async def _connect_shared(
        self,
        runtime: OrgRuntime,
        org_id: str,
        upstream: UpstreamDefinition,
        actor: str,
    ) -> StartResult:
        """Connect a service-account upstream's shared session, discover
        its tools, then record the outcome."""
        # Read after this Start's own Stop (``_start_shared`` disconnects
        # before it launches this task), so only a later one counts.
        stops_seen = runtime.client_manager.stop_count(upstream.id)
        error: str | None = None
        try:
            await runtime.client_manager.connect_upstream(upstream)
        except ConnectAborted:
            # A Stop aborted the connect this Start waited on. Stop owns
            # the state, so record nothing.
            return StartResult(started=False)
        except asyncio.CancelledError:
            # Stop or a second Start cancelled us; they own the state.
            raise
        except Exception as e:
            # Saved, audited and answered: an error quoting the URL or
            # command must not carry the password Variables in it.
            error = runtime.client_manager.hide_secrets_in_error(
                upstream.id, str(e) or e.__class__.__name__,
            )
        if error is None and self._stopped_meanwhile(
            runtime, upstream.id, stops_seen,
        ):
            return StartResult(started=False)
        store = self._deps.connection_store
        if store is not None:
            if error:
                await store.set_connection_error(org_id, upstream.id, error)
            else:
                await store.clear_connection_error(org_id, upstream.id)
        log_fields: dict[str, object] = {
            "upstream_id": upstream.id, "org_id": org_id,
        }
        discovery_error: str | None = None
        if error is None:
            try:
                await runtime.tool_registry.refresh_upstream(upstream.id)
            except Exception as exc:
                discovery_error = runtime.client_manager.hide_secrets_in_error(
                    upstream.id, str(exc) or exc.__class__.__name__,
                )
                logger.exception(
                    "upstream.admin.start.refresh_failed", **log_fields,
                )
            if self._stopped_meanwhile(runtime, upstream.id, stops_seen):
                return StartResult(started=False)
            logger.info("upstream.admin.start.success", **log_fields)
        else:
            logger.warning(
                "upstream.admin.start.failed", error=error, **log_fields,
            )
        # Either way ``ready`` changed: the listing refetches, and on a
        # failure picks up the new disconnect reason.
        publish_policy_changed(self._deps.event_bus, org_id)
        await log_admin_action(
            self._deps.audit_repo, org_id,
            action="reconnect",
            upstream_id=upstream.id,
            admin_email=actor,
            outcome="success" if error is None else "error",
            error_message=error,
        )
        return StartResult(
            started=error is None, error=error, discovery_error=discovery_error,
        )

    @runs_to_completion
    async def stop_upstream(
        self, org_id: str, upstream_id: str, *, actor: str,
    ) -> None:
        """The dashboard's Stop: close every live session to the upstream,
        the shared one and each user's own, and keep it stopped across
        restarts. Deletes no saved sign-in, so Start brings it back with
        nobody signing in again; Remove sign-in frees the admin slot."""
        runtime = await self._deps.runtime_manager.get(org_id)
        await self._upstream(runtime, org_id, upstream_id)
        logger.info(
            "upstream.admin.stop.requested",
            upstream_id=upstream_id, admin_email=actor, org_id=org_id,
        )
        try:
            await stop_keeping_sign_ins(
                org_id=org_id,
                upstream_id=upstream_id,
                client_manager=runtime.client_manager,
                connection_store=self._deps.connection_store,
            )
        except Exception as e:
            error = runtime.client_manager.hide_secrets_in_error(
                upstream_id, str(e),
            )
            logger.warning(
                "upstream.admin.stop.failed",
                upstream_id=upstream_id, org_id=org_id, error=error,
                exc_info=True,
            )
            await log_admin_action(
                self._deps.audit_repo, org_id,
                action="disconnect",
                upstream_id=upstream_id,
                admin_email=actor,
                outcome="error",
                error_message=error,
            )
            raise
        logger.info(
            "upstream.admin.stop.success",
            upstream_id=upstream_id, org_id=org_id,
        )
        publish_policy_changed(self._deps.event_bus, org_id)
        await log_admin_action(
            self._deps.audit_repo, org_id,
            action="disconnect",
            upstream_id=upstream_id,
            admin_email=actor,
            outcome="success",
        )

    @runs_to_completion
    async def remove_sign_in(
        self,
        org_id: str,
        upstream_id: str,
        *,
        actor: str,
        expected_email: str,
    ) -> str | None:
        """Remove sign-in (the dashboard): delete the admin sign-in the
        upstream shows (``slot_owner_of``), whoever holds it, so another
        admin can sign in with Authenticate (the take-over). Members' own
        sign-ins stay, and a stopped upstream stays stopped.

        ``expected_email`` is the admin the confirm dialog named. Only
        that sign-in goes: when another admin's is shown by now, raises
        ``SignInChanged`` and removes nothing. Already gone is not an
        error (two admins may click at once): returns ``None``. Returns
        the admin whose sign-in was removed.
        """
        runtime = await self._deps.runtime_manager.get(org_id)
        upstream = await self._upstream(runtime, org_id, upstream_id)
        if upstream.auth.mode == AuthMode.service_account:
            raise NoSignInNeeded()
        store = self._deps.connection_store
        if store is None:
            raise InvalidRequest("OAuth is not configured")
        owner = await slot_owner_of(
            store, org_id, upstream_id,
            admin_emails=runtime.policy_engine.get_admin_emails(),
        )
        if owner is not None and owner != expected_email:
            raise SignInChanged(expected_email, owner)
        if owner is not None:
            await sign_out_of_upstream(
                org_id=org_id,
                upstream_id=upstream_id,
                user_id=owner,
                connection_store=store,
                client_manager=runtime.client_manager,
            )
            publish_policy_changed(self._deps.event_bus, org_id)
        logger.info(
            "upstream.admin.sign_in.removed",
            upstream_id=upstream_id, org_id=org_id, removed=owner,
            admin_email=actor,
        )
        await log_admin_action(
            self._deps.audit_repo, org_id,
            action="sign_out",
            upstream_id=upstream_id,
            admin_email=actor,
            outcome="success" if owner is not None else "already_signed_out",
            target_user_id=expected_email,
        )
        return owner

    # --- Refresh tools ---

    async def refresh_tools(
        self, org_id: str, upstream_id: str, *, actor: str,
    ) -> asyncio.Future[str | None]:
        """Refresh tools: discover a ready upstream's tools again, in the
        background (after an E2B pause the discovery can stall ~15 s and
        reconnect on a fresh session; the dashboard must not wait for it).

        Refused (``Conflict``) while the upstream is not ready: this never
        connects a fresh session and never signs anyone in. An OAuth
        upstream is reattached from the sign-in of the admin it shows
        (``resolve_upstream_readiness``), not the caller's.

        The outcome is recorded either way: the upstream's error banner is
        set or cleared, sessions are told to list their tools again, and a
        ``refresh_tools`` row is audited. A failure tears nothing down: a
        refresh must not kill a warm sandbox or sign an admin out.

        Returns a future that resolves as soon as discovery ends: the
        error text, or ``None`` on success.
        """
        runtime = await self._deps.runtime_manager.get(org_id)
        upstream = await self._upstream(runtime, org_id, upstream_id)
        store = self._deps.connection_store
        ready, slot_owner = await resolve_upstream_readiness(
            upstream, org_id, store, runtime,
        )
        if not ready:
            raise Conflict("Upstream is not active")
        log_fields: dict[str, object] = {
            "upstream_id": upstream_id, "org_id": org_id,
        }
        logger.info(
            "upstream.admin.refresh_tools.requested",
            admin_email=actor, **log_fields,
        )
        discovered: asyncio.Future[str | None] = (
            asyncio.get_running_loop().create_future()
        )

        def _discovery_done(error: str | None) -> None:
            if not discovered.done():
                discovered.set_result(error)

        async def _record(error: str | None) -> None:
            if store is not None:
                if error is None:
                    await store.clear_connection_error(org_id, upstream_id)
                else:
                    await store.set_connection_error(org_id, upstream_id, error)
            publish_policy_changed(self._deps.event_bus, org_id)
            await log_admin_action(
                self._deps.audit_repo, org_id,
                action=REFRESH_TOOLS,
                upstream_id=upstream_id,
                admin_email=actor,
                outcome="success" if error is None else "error",
                error_message=error,
            )
            if error is None:
                logger.info("upstream.admin.refresh_tools.success", **log_fields)
            else:
                logger.warning(
                    "upstream.admin.refresh_tools.failed",
                    error=error, **log_fields,
                )

        async def _succeeded() -> None:
            await _record(None)

        task = refresh_tools_in_background(
            org_id=org_id,
            upstream=upstream,
            effective_user=slot_owner or actor,
            connection_store=store,
            client_manager=runtime.client_manager,
            tool_registry=runtime.tool_registry,
            server_url=self._deps.server_url,
            on_success=_succeeded,
            on_error=_record,
            on_discovery_done=_discovery_done,
        )
        # Settles the future when the refresh ended without reporting
        # (cancelled at shutdown).
        task.add_done_callback(lambda _: discovered.cancel())
        return discovered
