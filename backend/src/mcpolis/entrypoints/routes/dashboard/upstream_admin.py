"""Upstream-admin router (the load-bearing concern).

Lifecycle: list, get detail, logs, get tools, add,
update, remove, import (preview + confirm), connect (admin OAuth),
disconnect, reconnect.

The ``_build_upstream_summaries`` packer is a private module-level
helper used by ``list_upstreams``.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from mcpolis.adapters.repositories.upstream_config_loader import (
    build_upstream,
    extract_import_entries,
)
from mcpolis.domain.model.template_var import is_valid_template_var_name
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
    validate_stdio_uses_service_account,
)
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.services.plan_gates import resolve_plan
from mcpolis.domain.services.upstream_admin_service import (
    ImportRow,
    NewUpstreamRequest,
    SandboxSizeRequest,
    TemplateVarInput,
    check_sandbox_size,
    check_template_var_names,
    check_upstream_url,
    resolve_upstream_readiness,
)
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import (
    DashboardDeps,
    notify_policy_change,
    sse_encode,
)
from mcpolis.entrypoints.routes.dashboard._models import (
    AddUpstreamRequest,
    ConnectResponse,
    ConnectedUser,
    ImportConfirmEntry,
    ImportConfirmRequest,
    ImportDuplicateRef,
    ImportEntry,
    ImportErrorDetail,
    ImportFileRequest,
    ImportPreviewResponse,
    ImportResultResponse,
    SandboxResourcesView,
    SignOutRequest,
    ToolAnnotationsInfo,
    ToolInfo,
    UpdateUpstreamRequest,
    UpdateUpstreamTemplateVarChanges,
    UpstreamDetail,
    UpstreamSummary,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

async def _resolve_template_var_sets(
    deps: DashboardDeps,
    org_id: str,
    upstream_id: str,
    changes: UpdateUpstreamTemplateVarChanges,
) -> dict[str, tuple[str, bool]]:
    """Turn ``changes.sets`` into ``{name: (value, is_secret)}`` to write.

    A password is write-only, so the dashboard can't send its saved
    value back. It sends ``value=None`` instead, meaning "keep the
    saved value" of ``rename_from`` (a rename) or of the same name.
    Those values are read here, before any write, and a missing
    source rejects the whole save with a 400. Keeping a row's own
    value is a no-op, so it is left out of the result.

    A kept value always keeps its source's ``is_secret``. It may land
    on another existing row only when the same save deletes that row:
    :func:`_apply_template_var_changes` then recreates the row, so the
    source's flag holds. On a plain replace the existing row's flag
    would win, and a password moved onto a plain row would show in the
    list.
    """
    existing = {
        s.name: s
        for s in await deps.template_var_repo.list_summaries(
            org_id, upstream_id,
        )
    }
    deleted = set(changes.deletes)
    resolved: dict[str, tuple[str, bool]] = {}
    for var_name, spec in changes.sets.items():
        if spec.value is not None:
            resolved[var_name] = (spec.value, spec.is_secret)
            continue
        source = spec.rename_from or var_name
        if not is_valid_template_var_name(source):
            raise HTTPException(
                400,
                f"Invalid variable name {source!r}: must match "
                "[A-Z_][A-Z0-9_]*",
            )
        source_row = existing.get(source)
        value = await deps.template_var_repo.get_value(
            org_id, upstream_id, source,
        )
        if source_row is None or value is None:
            raise HTTPException(
                400,
                f"Variable {source!r} has no saved value to keep. "
                "Enter a value instead.",
            )
        if source == var_name:
            # Keep the row as it is. Rewriting it would bump
            # ``updated_at`` and raise the restart banner for nothing.
            continue
        if var_name in existing and var_name not in deleted:
            raise HTTPException(
                400,
                f"Cannot rename {source!r} to {var_name!r}: a variable "
                "with that name already exists.",
            )
        resolved[var_name] = (value, source_row.is_secret)
    return resolved


async def _apply_template_var_changes(
    repo: TemplateVarRepository,
    org_id: str,
    upstream_id: str,
    changes: UpdateUpstreamTemplateVarChanges,
    resolved: dict[str, tuple[str, bool]],
) -> None:
    """Write a save's Variable changes.

    Rows the save only deletes are removed last, so a failed write
    leaves a renamed password's source row in place: the dashboard
    never held that value, so nobody could send it again.

    A name in both ``sets`` and ``deletes`` is deleted right before its
    own write, so the row is created fresh with the flag ``resolved``
    gives it. A failed write there loses that row, which the save was
    replacing (in a swap, the old value it was moving too).
    """
    deleted = set(changes.deletes)
    for var_name, (value, is_secret) in resolved.items():
        if var_name in deleted:
            await repo.delete(org_id, upstream_id, var_name)
        await repo.set(
            org_id, upstream_id, var_name, value, is_secret=is_secret,
        )
    for var_name in deleted - changes.sets.keys():
        await repo.delete(org_id, upstream_id, var_name)


async def _build_upstream_summaries(
    deps: DashboardDeps,
    upstreams: list[UpstreamDefinition],
    disconnect_reasons: Mapping[str, str] | None = None,
) -> list[UpstreamSummary]:
    """Pack a list of upstreams into the wire shape ``list_upstreams``
    returns.

    Resolves readiness once per upstream up front; fills in persistent
    errors from the connection store for non-Ready upstreams that
    don't already have a reason.
    """
    org_id = current_org_id.get()
    runtime = await deps.runtime_manager.get(org_id)
    all_tools = runtime.tool_registry.get_all_tools()
    tool_counts: dict[str, int] = {}
    for t in all_tools:
        tool_counts[t.upstream_id] = tool_counts.get(t.upstream_id, 0) + 1

    reasons = dict(disconnect_reasons) if disconnect_reasons else {}

    readiness: dict[str, tuple[bool, str | None]] = {}
    for u in upstreams:
        readiness[u.id] = await resolve_upstream_readiness(
            u, org_id, deps.connection_store, runtime,
        )

    if deps.connection_store is not None:
        for u in upstreams:
            if readiness[u.id][0] or u.id in reasons:
                continue
            persistent_error = await deps.connection_store.get_connection_error(
                org_id, u.id,
            )
            if persistent_error:
                reasons[u.id] = persistent_error

    return [
        UpstreamSummary(
            id=u.id,
            display_name=u.display_name,
            transport=u.transport.value,
            auth_mode=u.auth.mode.value,
            ready=readiness[u.id][0],
            slot_owner=readiness[u.id][1],
            tool_count=tool_counts.get(u.id, 0),
            refreshing=runtime.tool_registry.is_refreshing(u.id),
            starting=runtime.client_manager.is_starting(u.id),
            stopped=runtime.client_manager.is_stopped(u.id),
            url=u.http.url if u.http else None,
            disconnect_reason=(
                reasons.get(u.id) if not readiness[u.id][0] else None
            ),
        )
        for u in upstreams
    ]


def create_upstream_admin_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get("/upstreams", response_model=list[UpstreamSummary])
    async def list_upstreams() -> list[UpstreamSummary]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstreams = await runtime.config_service.list_upstreams(org_id)

        # Detect disconnect reasons. Persistent errors are filled in
        # by ``_build_upstream_summaries``; here we cover the
        # token-expired case for OAuth modes by inspecting the slot
        # owner's row.
        reasons: dict[str, str] = {}
        if deps.connection_store is not None:
            now = datetime.now(UTC)
            for u in upstreams:
                if u.auth.mode == AuthMode.service_account:
                    continue
                if runtime.client_manager.is_stopped(u.id):
                    # Stopped is the whole story; an expired access
                    # token on the kept sign-in is refreshed at Start.
                    continue
                _, slot_owner = await resolve_upstream_readiness(
                    u, org_id, deps.connection_store, runtime,
                )
                if slot_owner is None:
                    continue
                token = await deps.connection_store.get_user_token(
                    org_id, slot_owner, u.id,
                )
                if (
                    token is not None
                    and token.expires_at is not None
                    and token.expires_at < now
                ):
                    reasons[u.id] = "token_expired"

        return await _build_upstream_summaries(deps, upstreams, reasons)

    @router.get(
        "/upstreams/{upstream_id}", response_model=UpstreamDetail,
    )
    async def get_upstream(upstream_id: str) -> UpstreamDetail:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")
        # Build the raw mcpServers config entry
        server_config: dict[str, Any] = {}
        if upstream.stdio:
            server_config["command"] = upstream.stdio.command
            if upstream.stdio.args:
                server_config["args"] = upstream.stdio.args
            if upstream.stdio.env:
                server_config["env"] = upstream.stdio.env
        elif upstream.http:
            server_config["url"] = upstream.http.url
            if upstream.http.headers:
                server_config["headers"] = upstream.http.headers

        ready, slot_owner = await resolve_upstream_readiness(
            upstream, org_id, deps.connection_store, runtime,
        )

        # Detect disconnect reason — persistent error first, then a
        # token-expired hint when the slot owner's row has expired.
        disconnect_reason: str | None = None
        if not ready and deps.connection_store is not None:
            persistent_error = await deps.connection_store.get_connection_error(
                org_id, upstream.id,
            )
            if persistent_error:
                disconnect_reason = persistent_error
        if (
            disconnect_reason is None
            and slot_owner is not None
            and not runtime.client_manager.is_stopped(upstream.id)
            and deps.connection_store is not None
        ):
            now = datetime.now(UTC)
            token = await deps.connection_store.get_user_token(
                org_id, slot_owner, upstream.id,
            )
            if (
                token is not None
                and token.expires_at is not None
                and token.expires_at < now
            ):
                disconnect_reason = "token_expired"

        # Connected users + per-user metadata (expires_at, is_admin).
        # Frontend uses these to render the three-state Connections
        # section and to surface admin badges.
        connected_users: list[ConnectedUser] = []
        if deps.connection_store is not None:
            emails = await deps.connection_store.get_connected_users(
                org_id, upstream.id,
            )
            admin_emails = set(runtime.policy_engine.get_admin_emails())
            for email in emails:
                token = await deps.connection_store.get_user_token(
                    org_id, email, upstream.id,
                )
                connected_users.append(
                    ConnectedUser(
                        email=email,
                        expires_at=(token.expires_at if token else None),
                        is_admin=email in admin_emails,
                    ),
                )

        # Dirty-banner inputs: compare the running session's snapshot
        # against a fresh recompute. ``is_dirty`` only fires when the
        # upstream is ready (a running session has to exist for there
        # to be drift); ``config_hash`` is exposed unconditionally so
        # the frontend can key its per-MCP dismissal by the current
        # saved state and re-show on the next save.
        live_config_hash = await runtime.client_manager.compute_runtime_hash(
            upstream,
        )
        started_config_hash = (
            await runtime.client_manager.get_started_config_hash(upstream.id)
        )
        is_dirty = (
            ready
            and started_config_hash is not None
            and started_config_hash != live_config_hash
        )

        return UpstreamDetail(
            id=upstream.id,
            display_name=upstream.display_name,
            transport=upstream.transport.value,
            auth_mode=upstream.auth.mode.value,
            ready=ready,
            slot_owner=slot_owner,
            starting=runtime.client_manager.is_starting(upstream.id),
            stopped=runtime.client_manager.is_stopped(upstream.id),
            url=upstream.http.url if upstream.http else None,
            command=upstream.stdio.command if upstream.stdio else None,
            client_id=upstream.auth.client_id,
            has_client_secret=upstream.auth.client_secret is not None,
            oauth_app_domain=upstream.auth.matched_domain,
            oauth_app_client_id=(
                upstream.auth.client_id
                if upstream.auth.matched_domain is not None
                else None
            ),
            scopes=upstream.auth.scopes,
            default_arguments=upstream.default_arguments,
            server_config=server_config,
            server_info=runtime.client_manager.get_server_info(upstream.id),
            disconnect_reason=disconnect_reason,
            connected_users=connected_users,
            sandbox_resources=(
                SandboxResourcesView(
                    cpu_vcpus=upstream.stdio.cpu_vcpus,
                    memory_mb=upstream.stdio.memory_mb,
                    disk_gb=upstream.stdio.disk_gb,
                    pids_limit=upstream.stdio.pids_limit,
                    tmpfs_mb=upstream.stdio.tmpfs_mb,
                    persistent_disk_enabled=upstream.stdio.persistent_disk_enabled,
                ) if upstream.stdio is not None else None
            ),
            is_dirty=is_dirty,
            config_hash=live_config_hash,
        )

    @router.get("/upstreams/{upstream_id}/logs")
    async def get_upstream_logs(upstream_id: str) -> dict[str, str | None]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")
        return {"logs": runtime.client_manager.get_log_output(upstream_id)}

    @router.get("/upstreams/{upstream_id}/logs/stream")
    async def stream_upstream_logs(upstream_id: str) -> StreamingResponse:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")
        if upstream.transport != TransportType.stdio:
            raise HTTPException(
                404, "Log streaming only applies to stdio upstreams",
            )
        # Eagerly create the buffer so the EventSource handshake
        # always returns 200, even for an upstream that has never
        # had a session. EventSource's auto-reconnect only fires
        # for connection drops *after* a successful handshake;
        # responding 404 here puts the client straight into CLOSED
        # with no retry, so the operator would see no logs until a
        # full page refresh. The buffer is just an in-memory ring
        # — creating it pre-Start has zero cost and matches the
        # precedent set by ``set_redactions``.
        log_buffer = runtime.client_manager.log_buffers.get_or_create(
            upstream_id,
        )

        async def event_stream() -> AsyncIterator[str]:
            async for chunk in log_buffer.subscribe():
                if chunk:
                    yield f"data: {sse_encode(chunk)}\n\n"
                else:
                    yield ": keepalive\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get(
        "/upstreams/{upstream_id}/tools", response_model=list[ToolInfo],
    )
    async def get_upstream_tools(upstream_id: str) -> list[ToolInfo]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")
        tools = runtime.tool_registry.get_tools_for_upstreams([upstream_id])
        return [
            ToolInfo(
                upstream_id=t.upstream_id,
                original_name=t.original_name,
                prefixed_name=t.prefixed_name,
                description=t.description,
                input_schema=t.input_schema,
                title=t.title,
                output_schema=t.output_schema,
                annotations=ToolAnnotationsInfo(**t.annotations.model_dump()) if t.annotations else None,
            )
            for t in tools
        ]

    @router.post(
        "/upstreams/{upstream_id}/refresh-tools",
        response_model=ConnectResponse,
    )
    async def refresh_upstream_tools_admin(
        upstream_id: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> ConnectResponse:
        """Re-pull an active upstream's tool list off its live session.

        Never initiates a *fresh* connection: a down or unauthenticated
        upstream is refused with 409 (``UpstreamAdminService.refresh_tools``,
        which the Admin MCP shares). When ready, the live session is
        reattached the same in-band way a tool call does before
        discovery, mirroring ``tool_router._resolve_session``:

        - ``service_account``: ``ensure_shared_connected`` lazily reopens
          a DEFERRED_ATTACH shared session (idempotent when LIVE).
        - OAuth: if no session is live yet, reconnect the slot owner's
          session from stored tokens (with refresh). Never prompts a
          browser flow.

        Non-blocking: the refresh (which can stall ~15s and reconnect on
        a fresh session after an E2B auto-pause) runs in the background
        and the request returns at once. The dashboard shows the
        "Fetching info" pill and learns the outcome via tools/list_changed
        and the upstream's error banner. This is the fix for the
        2026-06-18 prod incident, where the SYNCHRONOUS refresh surfaced a
        TimeoutError for a refresh that actually succeeded later.
        """
        await deps.upstream_admin.refresh_tools(
            current_org_id.get(), upstream_id, actor=admin_email,
        )
        return ConnectResponse(connected=True, upstream_id=upstream_id)

    @router.post(
        "/upstreams", response_model=UpstreamSummary, status_code=201,
    )
    async def add_upstream(
        body: AddUpstreamRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> UpstreamSummary:
        upstream = await deps.upstream_admin.add_upstream(
            current_org_id.get(),
            NewUpstreamRequest(
                id=body.id,
                display_name=body.display_name,
                url=body.url,
                headers=body.headers,
                command=body.command,
                args=body.args,
                env=body.env,
                sandbox=SandboxSizeRequest(
                    cpu_vcpus=body.cpu_vcpus,
                    memory_mb=body.memory_mb,
                    disk_gb=body.disk_gb,
                    pids_limit=body.pids_limit,
                    tmpfs_mb=body.tmpfs_mb,
                    persistent_disk_enabled=body.persistent_disk_enabled,
                ),
                auth_mode=body.auth_mode,
                auth_token=body.auth_token,
                client_id=body.client_id,
                client_secret=body.client_secret,
                scopes=body.scopes,
                template_vars={
                    name: TemplateVarInput(
                        value=spec.value, is_secret=spec.is_secret,
                    )
                    for name, spec in (body.template_vars or {}).items()
                },
            ),
            actor=admin_email,
            source="dashboard.add_upstream",
        )
        return UpstreamSummary(
            id=upstream.id,
            display_name=upstream.display_name,
            transport=upstream.transport.value,
            auth_mode=upstream.auth.mode.value,
            ready=False,
            slot_owner=None,
            tool_count=0,
            url=upstream.http.url if upstream.http else None,
        )

    @router.put("/upstreams/{upstream_id}", response_model=UpstreamDetail)
    async def update_upstream(
        upstream_id: str, body: UpdateUpstreamRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> UpstreamDetail:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")

        # Apply cosmetic changes
        if body.display_name is not None:
            upstream.display_name = body.display_name
        # Apply auth mode change
        if body.auth_mode is not None:
            # Stdio + non-service_account is a non-functional shape;
            # reject up front so the dashboard doesn't persist a config
            # that silently can't connect. Pydantic's
            # ``model_validator`` on ``UpstreamDefinition`` doesn't fire
            # on field assignment (default config), so the explicit
            # check is what backstops this endpoint.
            try:
                validate_stdio_uses_service_account(
                    upstream.transport, AuthMode(body.auth_mode),
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            upstream.auth = UpstreamAuthConfig(
                mode=AuthMode(body.auth_mode),
                client_id=upstream.auth.client_id,
                client_secret=upstream.auth.client_secret,
                scopes=upstream.auth.scopes,
            )
        # Apply OAuth client credentials
        if body.client_id is not None:
            upstream.auth.client_id = body.client_id or None
        if body.client_secret is not None:
            upstream.auth.client_secret = body.client_secret or None

        # Apply server config change
        server_config_changed = body.server_config is not None
        # Track whether we touched anything resource-related so we can
        # validate exactly once at the end (covers both server_config
        # rebuilds and the new ``sandbox_resources`` patch).
        resources_touched = False
        if server_config_changed:
            sc = body.server_config
            assert sc is not None
            if "command" in sc and not deps.allow_stdio_mcp:
                raise HTTPException(400, "Stdio MCP servers are disabled")
            if "command" in sc:
                upstream.transport = TransportType.stdio
                # Carry the existing stdio config's resource fields
                # forward when the body doesn't restate them. The
                # admin JSON editor only round-trips
                # ``command/args/env``, so without this fallback an
                # innocent JSON edit would silently reset CPU / RAM /
                # disk / pids / tmpfs / persistent_disk_enabled to
                # the model defaults — wiping a setting the operator
                # tuned via the resource picker.
                old_stdio = upstream.stdio
                stdio_cfg = StdioTransportConfig(
                    command=sc["command"],
                    args=sc.get("args", []),
                    env=sc.get("env", {}),
                    cpu_vcpus=float(
                        sc.get(
                            "cpu_vcpus",
                            old_stdio.cpu_vcpus if old_stdio else 1.0,
                        ),
                    ),
                    memory_mb=int(
                        sc.get(
                            "memory_mb",
                            old_stdio.memory_mb if old_stdio else 1024,
                        ),
                    ),
                    disk_gb=int(
                        sc.get(
                            "disk_gb",
                            old_stdio.disk_gb if old_stdio else 0,
                        ),
                    ),
                    pids_limit=(
                        int(sc["pids_limit"])
                        if sc.get("pids_limit") is not None
                        else (old_stdio.pids_limit if old_stdio else None)
                    ),
                    tmpfs_mb=(
                        int(sc["tmpfs_mb"])
                        if sc.get("tmpfs_mb") is not None
                        else (old_stdio.tmpfs_mb if old_stdio else None)
                    ),
                    persistent_disk_enabled=(
                        bool(sc["persistent_disk_enabled"])
                        if sc.get("persistent_disk_enabled") is not None
                        else (
                            old_stdio.persistent_disk_enabled
                            if old_stdio else False
                        )
                    ),
                )
                upstream.stdio = stdio_cfg
                upstream.http = None
                resources_touched = True
            elif "url" in sc:
                check_upstream_url(sc["url"])
                upstream.transport = TransportType.streamable_http
                upstream.http = HttpTransportConfig(
                    url=sc["url"],
                    headers=sc.get("headers", {}),
                )
                upstream.stdio = None
            else:
                raise HTTPException(
                    400, "server_config must contain 'url' or 'command'",
                )

        # Apply the ``sandbox_resources`` patch on top of whatever
        # the server_config branch just produced (or just on the
        # existing stdio config when no JSON edit was sent). Each
        # field is independently optional — None means "leave alone."
        if body.sandbox_resources is not None:
            if upstream.stdio is None:
                raise HTTPException(
                    400,
                    "sandbox_resources can only be set on stdio upstreams",
                )
            patch = body.sandbox_resources
            if patch.cpu_vcpus is not None:
                upstream.stdio.cpu_vcpus = patch.cpu_vcpus
            if patch.memory_mb is not None:
                upstream.stdio.memory_mb = patch.memory_mb
            if patch.disk_gb is not None:
                upstream.stdio.disk_gb = patch.disk_gb
            if patch.pids_limit is not None:
                upstream.stdio.pids_limit = patch.pids_limit
            if patch.tmpfs_mb is not None:
                upstream.stdio.tmpfs_mb = patch.tmpfs_mb
            if patch.persistent_disk_enabled is not None:
                upstream.stdio.persistent_disk_enabled = (
                    patch.persistent_disk_enabled
                )
            resources_touched = True

        # Single validation point: covers server_config rebuilds and
        # ``sandbox_resources`` patches. Off-grid → 400 with the
        # structured ``{message, field, value}`` the admin UI uses
        # to flag the offending control.
        if resources_touched and upstream.stdio is not None:
            # The plan check runs only when the patch actually touched
            # CPU or memory — picking just disk / pids / tmpfs /
            # persistent_disk_enabled doesn't relate to the plan's
            # matrix. The provider check always runs.
            patch_touched_combo = False
            if body.server_config is not None:
                sc = body.server_config
                patch_touched_combo = (
                    "cpu_vcpus" in sc or "memory_mb" in sc
                )
            if body.sandbox_resources is not None:
                patch_touched_combo = patch_touched_combo or (
                    body.sandbox_resources.cpu_vcpus is not None
                    or body.sandbox_resources.memory_mb is not None
                )
            await check_sandbox_size(
                runtime.client_manager,
                upstream.stdio,
                plan=(
                    await resolve_plan(deps.org_repo, org_id)
                    if patch_touched_combo else None
                ),
                source="dashboard.update_upstream",
                org_id=org_id,
                actor_email=admin_email,
            )

        # Validate template-var changes before any mutation so a bad
        # name rolls back the whole save without leaving half-applied
        # state. Names match the same regex the per-name PUT enforces.
        # Empty values are allowed (substitution emits the empty
        # string, sometimes the intended value).
        template_var_changes = body.template_var_changes
        if template_var_changes is not None:
            check_template_var_names(template_var_changes.sets)
            for var_name in template_var_changes.deletes:
                if not is_valid_template_var_name(var_name):
                    raise HTTPException(
                        400,
                        f"Invalid variable name {var_name!r}: must match "
                        "[A-Z_][A-Z0-9_]*",
                    )
        # Resolve every "keep the saved value" entry up front, before
        # any write: in a swap or a chain of renames, a source row is
        # rewritten during the apply step.
        resolved_template_vars: dict[str, tuple[str, bool]] = (
            await _resolve_template_var_sets(
                deps, org_id, upstream_id, template_var_changes,
            )
            if template_var_changes is not None
            else {}
        )

        # Save the new config WITHOUT touching the running session.
        # Even an auth_mode or server_config change leaves the live
        # MCP alone — the dashboard's ``DirtyConfigBanner`` (driven
        # by ``UpstreamDetail.is_dirty`` + ``config_hash``) tells the
        # operator to Stop+Start when they're ready to apply the
        # change. Tokens and persistent connection_error stay put;
        # the operator's next explicit Stop/Start handles their
        # lifecycle. This matches the behaviour resource edits
        # already had — picking the gentler path is what users
        # expect when they tweak config of a healthy MCP.
        if server_config_changed:
            assert body.server_config is not None
            await runtime.config_service.update_upstream_with_server_config(
                org_id, upstream, body.server_config,
            )
        else:
            await runtime.config_service.update_upstream(org_id, upstream)
        # Notify so connected dashboards refetch and the dirty banner
        # picks up the new ``is_dirty=true``. Cheap broadcast even
        # for cosmetic-only edits (display-name change still wants
        # to refresh the upstream listing).
        notify_policy_change(deps)

        # Apply env-var changes after the upstream config write so a
        # config validation failure doesn't strand env-var mutations.
        if template_var_changes is not None:
            await _apply_template_var_changes(
                deps.template_var_repo, org_id, upstream_id,
                template_var_changes, resolved_template_vars,
            )

        return await get_upstream(upstream_id)

    @router.delete("/upstreams/{upstream_id}")
    async def remove_upstream(
        upstream_id: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        await deps.upstream_admin.remove_upstream(
            current_org_id.get(), upstream_id, actor=admin_email,
        )
        return {"status": "removed"}

    @router.post(
        "/upstreams/import/preview", response_model=ImportPreviewResponse,
    )
    async def import_preview(
        body: ImportFileRequest,
    ) -> ImportPreviewResponse:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        data = body.data
        existing_ids = [
            u.id for u in await runtime.config_service.list_upstreams(org_id)
        ]
        parsed = extract_import_entries(data, existing_ids)
        if not parsed:
            # Single MCP entry (top-level url/command) — point the operator
            # at "Add MCP". Otherwise there's no recognizable wrapper.
            if "url" in data or "command" in data:
                raise HTTPException(
                    400,
                    "This looks like a single MCP entry, not a config file. "
                    "Use 'Add MCP' with JSON mode instead.",
                )
            raise HTTPException(
                400,
                "No MCP servers found. Expected a JSON file with a "
                "'mcpServers' or 'servers' key, or a Claude '.claude.json' "
                "with project-scoped servers.",
            )

        entries: list[ImportEntry] = []
        parse_errors: list[str] = []
        for parsed_entry in parsed:
            try:
                # Derive display_name / transport / auth from the *proposed*
                # id so the preview matches what confirm will create.
                upstream = build_upstream(
                    parsed_entry.proposed_id, parsed_entry.config, {},
                )
                is_stdio_blocked = (
                    not deps.allow_stdio_mcp
                    and upstream.transport == TransportType.stdio
                )
                entries.append(ImportEntry(
                    scope=parsed_entry.scope,
                    project_path=parsed_entry.project_path,
                    group_label=parsed_entry.group_label,
                    original_id=parsed_entry.original_id,
                    proposed_id=parsed_entry.proposed_id,
                    display_name=upstream.display_name,
                    transport=upstream.transport.value,
                    auth_mode=upstream.auth.mode.value,
                    blocked=is_stdio_blocked,
                    blocked_reason=(
                        "Stdio MCP servers are disabled"
                        if is_stdio_blocked else None
                    ),
                    duplicate_of=(
                        ImportDuplicateRef(
                            proposed_id=parsed_entry.duplicate_of.proposed_id,
                            group_label=parsed_entry.duplicate_of.group_label,
                        )
                        if parsed_entry.duplicate_of else None
                    ),
                ))
            except Exception as e:
                parse_errors.append(f"'{parsed_entry.original_id}': {e}")

        return ImportPreviewResponse(
            entries=entries,
            existing_ids=existing_ids,
            parse_errors=parse_errors,
        )

    @router.post(
        "/upstreams/import/confirm", response_model=ImportResultResponse,
    )
    async def import_confirm(
        body: ImportConfirmRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> ImportResultResponse:
        data = body.data

        def resolve_config(
            entry: ImportConfirmEntry,
        ) -> dict[str, Any] | None:
            """Re-resolve a row's raw server config from the blob by scope.

            Keeps the raw config server-side (the dialog only sends the
            id mapping) and guarantees preview/confirm read the same source.
            """
            servers: Any
            if entry.scope == "project":
                projects = data.get("projects")
                if entry.project_path is None or not isinstance(projects, dict):
                    return None
                proj = cast("dict[str, Any]", projects).get(entry.project_path)
                if not isinstance(proj, dict):
                    return None
                servers = cast("dict[str, Any]", proj).get("mcpServers")
            elif entry.scope == "user":
                servers = data.get("mcpServers")
            else:
                servers = data.get("mcpServers")
                if not isinstance(servers, dict):
                    servers = data.get("servers")
            if not isinstance(servers, dict):
                return None
            config = cast("dict[str, Any]", servers).get(entry.original_id)
            return (
                cast("dict[str, Any]", config)
                if isinstance(config, dict) else None
            )

        outcome = await deps.upstream_admin.import_upstreams(
            current_org_id.get(),
            [
                ImportRow(
                    original_id=entry.original_id,
                    target_id=entry.target_id,
                    config=resolve_config(entry),
                )
                for entry in body.entries
            ],
            actor=admin_email,
        )
        return ImportResultResponse(
            added=outcome.added,
            skipped=[],
            errors=[
                ImportErrorDetail(id=e.id, error=e.error)
                for e in outcome.errors
            ],
        )

    @router.post(
        "/upstreams/{upstream_id}/connect", response_model=ConnectResponse,
    )
    async def connect_upstream_admin(
        upstream_id: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> ConnectResponse:
        result = await deps.upstream_admin.connect_upstream(
            current_org_id.get(), upstream_id, actor=admin_email,
        )
        return ConnectResponse(
            authorization_url=result.authorization_url,
            connected=result.connected,
            error=result.error,
        )

    @router.post("/upstreams/{upstream_id}/sign-out")
    async def sign_out_upstream_admin(
        upstream_id: str,
        body: SignOutRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str | None]:
        """Remove sign-in: delete the admin sign-in the upstream shows,
        if it is still ``body.email``'s (409 when another admin's is
        shown by now). See ``UpstreamAdminService.remove_sign_in``."""
        removed = await deps.upstream_admin.remove_sign_in(
            current_org_id.get(), upstream_id,
            actor=admin_email, expected_email=body.email,
        )
        return {"status": "signed_out", "email": removed}

    @router.post("/upstreams/{upstream_id}/disconnect")
    async def disconnect_upstream_admin(
        upstream_id: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        await deps.upstream_admin.stop_upstream(
            current_org_id.get(), upstream_id, actor=admin_email,
        )
        return {"status": "disconnected"}

    @router.post(
        "/upstreams/{upstream_id}/reconnect",
        response_model=ConnectResponse,
    )
    async def reconnect_upstream_admin(
        upstream_id: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> ConnectResponse:
        """Start any upstream (all auth modes). A service-account Start
        runs in the background: the answer is ``pending`` and the
        ``sandbox_state_changed`` stream carries warming → active →
        ready / failed."""
        outcome = await deps.upstream_admin.start_upstream(
            current_org_id.get(), upstream_id, actor=admin_email,
        )
        if outcome.sign_in is None:
            return ConnectResponse(outcome="pending", upstream_id=upstream_id)
        return ConnectResponse(
            authorization_url=outcome.sign_in.authorization_url,
            connected=outcome.sign_in.connected,
            error=outcome.sign_in.error,
        )

    return router
