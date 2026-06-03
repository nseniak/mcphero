"""Upstream-admin router (14 routes — the load-bearing concern).

Lifecycle: list, get detail, refresh status, logs, get tools, add,
update, remove, import (preview + confirm), connect (admin OAuth),
disconnect, reconnect.

The ``_build_upstream_summaries`` packer used to be a closure inside
``create_dashboard_api_router`` — used by ``list_upstreams`` and
``refresh_upstream_status``; now a private module-level helper.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.adapters.repositories.upstream_config_loader import (
    build_upstream,
    extract_import_entries,
)
from mcpolis.domain.model.events import Event
from mcpolis.domain.model.template_var import is_valid_template_var_name
from mcpolis.domain.services.system_variables import is_system_variable_name
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
    validate_stdio_uses_service_account,
)
from mcpolis.domain.services.plan_gates import (
    assert_http_upstream_capacity,
    assert_sandbox_combo_allowed,
    assert_stdio_upstream_capacity,
    resolve_plan,
)
from mcpolis.domain.services.upstream_connection_service import (
    OAuthFailureReason,
    SessionUnavailable,
    acquire_and_refresh_with_recovery,
    connect_and_refresh_tools,
    reconnect_all_oauth_upstreams,
)
from mcpolis.domain.services.url_safety import (
    UnsafeUpstreamUrl,
    validate_upstream_url,
)
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import (
    DashboardDeps,
    admin_oauth_owner,
    log_admin_action,
    notify_policy_change,
    resolve_upstream_readiness,
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
    ToolAnnotationsInfo,
    ToolInfo,
    UpdateUpstreamRequest,
    UpstreamDetail,
    UpstreamSummary,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Upstream ids become tool-name prefixes (``{upstream}__{tool}``) and storage
# keys, so a confirmed import id must stay within the dashboard IdInput
# charset even when a scripted caller bypasses the UI.
_VALID_UPSTREAM_ID = re.compile(r"[a-z0-9._-]+")


async def _build_upstream_summaries(
    deps: DashboardDeps,
    upstreams: list[UpstreamDefinition],
    disconnect_reasons: Mapping[str, str] | None = None,
) -> list[UpstreamSummary]:
    """Pack a list of upstreams into the wire shape both
    ``list_upstreams`` and ``refresh_upstream_status`` return.

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

    @router.post(
        "/upstreams/refresh-status",
        response_model=list[UpstreamSummary],
    )
    async def refresh_upstream_status() -> list[UpstreamSummary]:
        """Try to reconnect disconnected OAuth upstreams; return updated list."""
        if deps.connection_store is None:
            return await list_upstreams()

        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        all_upstreams = await runtime.config_service.list_upstreams(org_id)
        reasons = await reconnect_all_oauth_upstreams(
            org_id,
            all_upstreams, deps.connection_store, runtime.client_manager,
            runtime.tool_registry, deps.server_url,
        )
        return await _build_upstream_summaries(deps, all_upstreams, reasons)

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

        Never initiates a *fresh* connection. The gate is the same
        readiness the dashboard uses to enable the button (admin
        authenticated for OAuth; shared session live/deferred for
        service_account), so a down/unauthenticated upstream is refused
        with 409 instead of being connected. When ready, the live
        session is reattached the same in-band way a tool call does
        before discovery — mirroring ``tool_router._resolve_session``:

        - ``service_account``: ``ensure_shared_connected`` lazily reopens
          a DEFERRED_ATTACH shared session (idempotent when LIVE).
        - OAuth: if no session is live yet, reconnect the slot owner's
          session from stored tokens (with refresh). Never prompts a
          browser flow.

        On failure the reason is recorded + broadcast (error banner +
        dashboard popup) but the session is deliberately NOT torn down:
        a refresh failure must not kill a warm sandbox or sign an OAuth
        admin out. Genuine unreachability already flips service_account
        to FAILED inside ``ensure_shared_connected``.
        """
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")

        ready, slot_owner = await resolve_upstream_readiness(
            upstream, org_id, deps.connection_store, runtime,
        )
        if not ready:
            raise HTTPException(409, "Upstream is not active")

        logger.info(
            "dashboard.api.admin.upstream.refresh_tools.requested",
            upstream_id=upstream_id,
            admin_email=admin_email,
            org_id=org_id,
        )

        try:
            # Reattach the live session exactly as a tool call does —
            # shared helper, so refresh and dispatch never drift. For
            # OAuth the slot owner (the admin holding the token) is the
            # effective user; service_account ignores it. Never prompts
            # a browser flow; raises SessionUnavailable if it can't.
            # ``_with_recovery`` retries on a transport stall (E2B's
            # post-reattach stdout stall) by reconnecting on a fresh
            # session, so a flaky reattach yields a complete catalogue
            # instead of a partial one or a 30s hang.
            await acquire_and_refresh_with_recovery(
                org_id=org_id,
                upstream=upstream,
                effective_user=slot_owner or admin_email,
                connection_store=deps.connection_store,
                client_manager=runtime.client_manager,
                tool_registry=runtime.tool_registry,
                server_url=deps.server_url,
            )
        except Exception as e:
            if isinstance(e, SessionUnavailable):
                error_msg = f"could not reattach session: {e.reason}"
            else:
                error_msg = str(e) or e.__class__.__name__
            # Record + broadcast so the dashboard shows the failure
            # (error banner + popup). Non-destructive on purpose.
            if deps.connection_store is not None:
                await deps.connection_store.set_connection_error(
                    org_id, upstream_id, error_msg,
                )
            notify_policy_change(deps)
            await log_admin_action(
                deps,
                action="refresh_tools",
                upstream_id=upstream_id,
                admin_email=admin_email,
                outcome="error",
                error_message=error_msg,
            )
            logger.warning(
                "dashboard.api.admin.upstream.refresh_tools.failed",
                upstream_id=upstream_id,
                org_id=org_id,
                error=error_msg,
            )
            return ConnectResponse(connected=False, error=error_msg)

        if deps.connection_store is not None:
            await deps.connection_store.clear_connection_error(
                org_id, upstream_id,
            )
        notify_policy_change(deps)
        await log_admin_action(
            deps,
            action="refresh_tools",
            upstream_id=upstream_id,
            admin_email=admin_email,
            outcome="success",
        )
        logger.info(
            "dashboard.api.admin.upstream.refresh_tools.success",
            upstream_id=upstream_id,
            org_id=org_id,
        )
        return ConnectResponse(connected=True, upstream_id=upstream_id)

    @router.post(
        "/upstreams", response_model=UpstreamSummary, status_code=201,
    )
    async def add_upstream(
        body: AddUpstreamRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> UpstreamSummary:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        if not body.url and not body.command:
            raise HTTPException(400, "Either 'url' or 'command' is required")
        if body.command and not deps.allow_stdio_mcp:
            raise HTTPException(400, "Stdio MCP servers are disabled")

        # Plan gate: count existing upstreams by transport before
        # any work happens. Sandbox-combo gate runs further down once
        # the resolved stdio config is in hand.
        plan = await resolve_plan(deps.org_repo, org_id)
        existing_upstreams = await runtime.config_service.list_upstreams(org_id)
        if body.command:
            current_stdio = sum(
                1 for u in existing_upstreams
                if u.transport == TransportType.stdio
            )
            assert_stdio_upstream_capacity(
                plan, current_stdio,
                source="dashboard.add_upstream",
                org_id=org_id,
                actor_email=admin_email,
            )
        else:
            current_http = sum(
                1 for u in existing_upstreams
                if u.transport == TransportType.streamable_http
            )
            assert_http_upstream_capacity(
                plan, current_http,
                source="dashboard.add_upstream",
                org_id=org_id,
                actor_email=admin_email,
            )

        try:
            validate_stdio_uses_service_account(
                TransportType.stdio if body.command else TransportType.streamable_http,
                AuthMode(body.auth_mode),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

        auth = UpstreamAuthConfig(
            mode=AuthMode(body.auth_mode),
            token=body.auth_token if body.auth_token else None,
            client_id=body.client_id,
            client_secret=body.client_secret,
            scopes=body.scopes,
        )

        if body.command:
            # Resource fields are optional: missing values fall through
            # to ``StdioTransportConfig``'s built-in defaults (1 vCPU /
            # 1024 MB / 0 disk). When any are supplied, validate the
            # resulting combo against the active provider's grid so an
            # off-grid value is rejected up front with a structured
            # 400 the admin form can flag.
            stdio_kwargs: dict[str, object] = {
                "command": body.command,
                "args": body.args,
                "env": body.env,
            }
            if body.cpu_vcpus is not None:
                stdio_kwargs["cpu_vcpus"] = body.cpu_vcpus
            if body.memory_mb is not None:
                stdio_kwargs["memory_mb"] = body.memory_mb
            if body.disk_gb is not None:
                stdio_kwargs["disk_gb"] = body.disk_gb
            if body.pids_limit is not None:
                stdio_kwargs["pids_limit"] = body.pids_limit
            if body.tmpfs_mb is not None:
                stdio_kwargs["tmpfs_mb"] = body.tmpfs_mb
            if body.persistent_disk_enabled is not None:
                stdio_kwargs["persistent_disk_enabled"] = body.persistent_disk_enabled
            stdio_cfg = StdioTransportConfig(**stdio_kwargs)  # type: ignore[arg-type]
            from mcpolis.domain.services.sandbox_service import (
                ResourcesUnsupported,
                SandboxResources,
            )
            try:
                caps = await runtime.client_manager.get_active_capabilities()
                services = runtime.client_manager._sandbox_services  # type: ignore[reportPrivateUsage]
                services[caps.provider].validate_resources(
                    SandboxResources(
                        cpu_vcpus=stdio_cfg.cpu_vcpus,
                        memory_mb=stdio_cfg.memory_mb,
                        disk_gb=stdio_cfg.disk_gb,
                        pids_limit=stdio_cfg.pids_limit,
                    ),
                )
            except ResourcesUnsupported as exc:
                raise HTTPException(
                    400,
                    detail={
                        "message": str(exc),
                        "field": exc.field,
                        "value": str(exc.value),
                    },
                ) from None
            # Sandbox-combo plan gate: provider-grid validation runs
            # FIRST so off-grid values get the more-specific 400 +
            # field hint. Only firing here when the admin actively
            # picked a value lets a no-resource create accept the
            # model default (1 vCPU / 1024 MB) without hitting a 402,
            # which matches the historical behaviour that callers
            # depend on.
            user_picked_combo = (
                body.cpu_vcpus is not None or body.memory_mb is not None
            )
            if user_picked_combo:
                assert_sandbox_combo_allowed(
                    plan,
                    stdio_cfg.cpu_vcpus,
                    stdio_cfg.memory_mb,
                    source="dashboard.add_upstream",
                    org_id=org_id,
                    actor_email=admin_email,
                )
            upstream = UpstreamDefinition(
                id=body.id,
                display_name=body.display_name,
                transport=TransportType.stdio,
                stdio=stdio_cfg,
                auth=auth,
            )
        else:
            assert body.url is not None
            try:
                validate_upstream_url(body.url)
            except UnsafeUpstreamUrl as exc:
                raise HTTPException(
                    400,
                    detail={
                        "code": "UNSAFE_UPSTREAM_URL",
                        "message": (
                            "This URL targets a private/loopback range "
                            "and cannot be used as an upstream MCP."
                        ),
                        "reason": exc.reason,
                    },
                ) from None
            upstream = UpstreamDefinition(
                id=body.id,
                display_name=body.display_name,
                transport=TransportType.streamable_http,
                http=HttpTransportConfig(
                    url=body.url,
                    headers=body.headers,
                ),
                auth=auth,
            )
        try:
            await runtime.config_service.add_upstream(org_id, upstream)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        # Persist any env vars the wizard collected before the create
        # call. Each entry carries an ``is_secret`` flag (default
        # ``True``) — secret values are masked after save, plain
        # values stay visible. Names are validated against the env-var
        # regex.
        if body.template_vars:
            for var_name, spec in body.template_vars.items():
                if not is_valid_template_var_name(var_name):
                    raise HTTPException(
                        400,
                        f"Invalid variable name {var_name!r}: must match "
                        "[A-Z_][A-Z0-9_]*",
                    )
                if is_system_variable_name(var_name):
                    raise HTTPException(
                        400,
                        f"Cannot create a Variable named {var_name!r}: "
                        "that name is reserved for the read-only system "
                        f"variable ${{{var_name}}}",
                    )
                await deps.template_var_repo.set(
                    org_id, upstream.id, var_name, spec.value,
                    is_secret=spec.is_secret,
                )
        # Create per-role access entries based on auto_enable_new
        new_config = await deps.policy_store.create_mcp_access(
            org_id, upstream.id,
        )
        runtime.policy_engine.reload(new_config)
        # Mark as disabled so it won't auto-connect on restart.
        # ``set_disabled`` writes an explicit ``enabled: False`` —
        # ``clear_enabled`` would only remove the marker, which
        # falls back to default-enabled and (until the boot gate
        # was tightened) silently auto-connected new upstreams
        # against the comment's intent.
        if deps.connection_store is not None:
            await deps.connection_store.set_disabled(org_id, upstream.id)
        get_analytics().track_async(
            admin_email,
            "upstream_added",
            {
                "upstream_id": upstream.id,
                "transport": upstream.transport.value,
                "auth_mode": upstream.auth.mode.value,
            },
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
                token=upstream.auth.token,
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
                try:
                    validate_upstream_url(sc["url"])
                except UnsafeUpstreamUrl as exc:
                    raise HTTPException(
                        400,
                        detail={
                            "code": "UNSAFE_UPSTREAM_URL",
                            "message": (
                                "This URL targets a private/loopback "
                                "range and cannot be used as an "
                                "upstream MCP."
                            ),
                            "reason": exc.reason,
                        },
                    ) from None
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
            from mcpolis.domain.services.sandbox_service import (
                ResourcesUnsupported,
                SandboxResources,
            )
            try:
                caps = await runtime.client_manager.get_active_capabilities()
                provider_name = caps.provider
                services = runtime.client_manager._sandbox_services  # type: ignore[reportPrivateUsage]
                services[provider_name].validate_resources(
                    SandboxResources(
                        cpu_vcpus=upstream.stdio.cpu_vcpus,
                        memory_mb=upstream.stdio.memory_mb,
                        disk_gb=upstream.stdio.disk_gb,
                        pids_limit=upstream.stdio.pids_limit,
                    ),
                )
            except ResourcesUnsupported as exc:
                raise HTTPException(
                    400,
                    detail={
                        "message": str(exc),
                        "field": exc.field,
                        "value": str(exc.value),
                    },
                ) from None
            # Plan combo gate fires only when the patch actually
            # touched CPU or memory — picking just disk / pids /
            # tmpfs / persistent_disk_enabled doesn't relate to the
            # plan's matrix. Provider-grid validation runs first so
            # off-grid values still get the more-specific 400.
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
            if patch_touched_combo:
                plan = await resolve_plan(deps.org_repo, org_id)
                assert_sandbox_combo_allowed(
                    plan,
                    upstream.stdio.cpu_vcpus,
                    upstream.stdio.memory_mb,
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
            for var_name in template_var_changes.sets:
                if not is_valid_template_var_name(var_name):
                    raise HTTPException(
                        400,
                        f"Invalid variable name {var_name!r}: must match "
                        "[A-Z_][A-Z0-9_]*",
                    )
                if is_system_variable_name(var_name):
                    raise HTTPException(
                        400,
                        f"Cannot create a Variable named {var_name!r}: "
                        "that name is reserved for the read-only "
                        f"system variable ${{{var_name}}}",
                    )
            for var_name in template_var_changes.deletes:
                if not is_valid_template_var_name(var_name):
                    raise HTTPException(
                        400,
                        f"Invalid variable name {var_name!r}: must match "
                        "[A-Z_][A-Z0-9_]*",
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
        # Deletes apply before sets so a same-name pair lands as the
        # set (intuitive: the buffer's last write wins on the row).
        if template_var_changes is not None:
            delete_set = set(template_var_changes.deletes)
            for var_name in delete_set - set(template_var_changes.sets.keys()):
                await deps.template_var_repo.delete(
                    org_id, upstream_id, var_name,
                )
            for var_name, spec in template_var_changes.sets.items():
                await deps.template_var_repo.set(
                    org_id, upstream_id, var_name, spec.value,
                    is_secret=spec.is_secret,
                )

        return await get_upstream(upstream_id)

    @router.delete("/upstreams/{upstream_id}")
    async def remove_upstream(
        upstream_id: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        try:
            await runtime.config_service.remove_upstream(org_id, upstream_id)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        # Provider-side teardown (E2B Volumes, persistence refs).
        # Runs after config removal so a partial failure here can't
        # leave the upstream half-deleted. on_upstream_removed is
        # idempotent across all backends so a future reconciliation
        # can still clean up if this fails transiently.
        try:
            await runtime.client_manager.cleanup_sandbox_state_for_upstream(
                upstream_id,
            )
        except Exception:
            logger.warning(
                "upstream.remove.sandbox_cleanup_failed",
                org_id=org_id, upstream_id=upstream_id, exc_info=True,
            )
        if deps.connection_store is not None:
            await deps.connection_store.clear_connection_error(
                org_id, upstream_id,
            )
            # Bistate (Phase E): set_enabled removes any explicit-
            # disabled marker so a fresh re-add starts in the
            # default-enabled state.
            await deps.connection_store.set_enabled(org_id, upstream_id)
        # The upstream's prefixed tools are gone from ToolRegistry; push
        # so connected clients stop listing them.
        notify_policy_change(deps)
        get_analytics().track_async(
            admin_email,
            "upstream_removed",
            {
                "upstream_id": upstream_id,
                "transport": upstream.transport.value if upstream else "unknown",
                "auth_mode": upstream.auth.mode.value if upstream else "unknown",
            },
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
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        existing_upstreams = await runtime.config_service.list_upstreams(org_id)
        existing = {u.id for u in existing_upstreams}
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

        added: list[str] = []
        skipped: list[str] = []
        errors: list[ImportErrorDetail] = []

        # Resolve + validate each row up front so the plan gate counts only
        # what will actually be created. Per-row problems become row errors;
        # the plan-cap 402 still stops the whole import.
        valid: list[tuple[ImportConfirmEntry, dict[str, Any]]] = []
        seen_targets: set[str] = set()
        for entry in body.entries:
            tid = entry.target_id
            if not tid or not _VALID_UPSTREAM_ID.fullmatch(tid):
                errors.append(ImportErrorDetail(
                    id=tid or entry.original_id,
                    error=(
                        "Invalid id (allowed: lowercase letters, digits, "
                        "hyphens, underscores, dots)"
                    ),
                ))
                continue
            if tid in existing:
                errors.append(ImportErrorDetail(
                    id=tid, error="An upstream with this id already exists",
                ))
                continue
            if tid in seen_targets:
                errors.append(ImportErrorDetail(
                    id=tid, error="Duplicate id in this import",
                ))
                continue
            config = resolve_config(entry)
            if config is None:
                errors.append(ImportErrorDetail(
                    id=tid,
                    error="Source server not found in the uploaded config",
                ))
                continue
            if not deps.allow_stdio_mcp and "command" in config:
                errors.append(ImportErrorDetail(
                    id=tid, error="Stdio MCP servers are disabled",
                ))
                continue
            seen_targets.add(tid)
            valid.append((entry, config))

        # Plan gate over the to-be-created set so a bulk import can't smuggle
        # past the per-add gate. The 402 stops the whole import — an explicit
        # admin action, so one popover beats partial success.
        plan = await resolve_plan(deps.org_repo, org_id)
        running_http = sum(
            1 for u in existing_upstreams
            if u.transport == TransportType.streamable_http
        )
        running_stdio = sum(
            1 for u in existing_upstreams
            if u.transport == TransportType.stdio
        )
        for _entry, config in valid:
            if "command" in config:
                assert_stdio_upstream_capacity(
                    plan, running_stdio,
                    source="dashboard.import_confirm",
                    org_id=org_id,
                    actor_email=admin_email,
                )
                running_stdio += 1
            elif "url" in config:
                assert_http_upstream_capacity(
                    plan, running_http,
                    source="dashboard.import_confirm",
                    org_id=org_id,
                    actor_email=admin_email,
                )
                running_http += 1

        for entry, config in valid:
            tid = entry.target_id
            try:
                upstream = build_upstream(tid, config, {})
                if upstream.http is not None:
                    try:
                        validate_upstream_url(upstream.http.url)
                    except UnsafeUpstreamUrl as exc:
                        errors.append(ImportErrorDetail(
                            id=tid,
                            error=(
                                f"UNSAFE_UPSTREAM_URL: {exc.reason}"
                            ),
                        ))
                        continue
                await runtime.config_service.add_upstream_no_refresh(
                    org_id, upstream,
                )
                new_config = await deps.policy_store.create_mcp_access(
                    org_id, tid,
                )
                runtime.policy_engine.reload(new_config)
                if deps.connection_store is not None:
                    # Same off-by-default intent as ``add_upstream``;
                    # explicit False survives restart.
                    await deps.connection_store.set_disabled(org_id, tid)
                added.append(tid)
                existing.add(tid)
            except Exception as e:
                errors.append(ImportErrorDetail(id=tid, error=str(e)))

        return ImportResultResponse(
            added=added, skipped=skipped, errors=errors,
        )

    @router.post(
        "/upstreams/{upstream_id}/connect", response_model=ConnectResponse,
    )
    async def connect_upstream_admin(
        upstream_id: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> ConnectResponse:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        if deps.connection_store is None or deps.auth_coordinator is None:
            raise HTTPException(400, "OAuth is not configured")
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")
        if upstream.auth.mode == AuthMode.service_account:
            raise HTTPException(400, "This MCP uses service_account auth")

        # Uniform single-slot UX across both OAuth modes: another
        # admin already holding a row must be explicitly disconnected
        # before this admin can take over. The frontend surfaces the
        # conflict and routes users to the disconnect-then-connect
        # flow. (per_user_oauth's storage allows multiple admin rows
        # in principle, but the admin-tab UX deliberately collapses
        # them into a single visible slot — same as admin_oauth.)
        existing = await admin_oauth_owner(
            deps.connection_store, org_id, upstream_id, admin_email,
            policy_engine=runtime.policy_engine,
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{existing}' is already connected to this MCP. "
                    "Disconnect first to take over."
                ),
            )

        logger.info(
            "dashboard.api.admin.upstream.connect.requested",
            upstream_id=upstream_id,
            admin_email=admin_email,
            org_id=org_id,
        )
        # Bistate: removes any explicit-disabled marker so the
        # upstream falls back to default-enabled. (Previously this
        # wrote ``enabled: True`` — under the bistate semantic
        # there's no such row; absence is the canonical enabled.)
        await deps.connection_store.set_enabled(org_id, upstream_id)

        async def _snapshot_started_config_hash() -> None:
            # OAuth-mode upstreams: see the equivalent helper in the
            # /reconnect handler below for the rationale. Snapshots
            # the current saved-config hash so the next config edit
            # flips ``is_dirty=true`` even before any session task
            # is created.
            if deps.connection_store is None:
                return
            hash_value = await runtime.client_manager.compute_runtime_hash(
                upstream,
            )
            try:
                await deps.connection_store.set_started_config_hash(
                    org_id, upstream_id, hash_value,
                )
            except Exception:
                logger.warning(
                    "dashboard.api.admin.upstream.connect."
                    "snapshot_hash_failed",
                    upstream_id=upstream_id,
                    org_id=org_id,
                    exc_info=True,
                )

        def _notify_tokens_acquired() -> None:
            if deps.event_bus is not None:
                deps.event_bus.publish(org_id, Event(
                    type="upstream_tokens_acquired",
                    payload={"upstream_id": upstream_id},
                ))
            # Slow path: tokens arrived via OAuth callback after the
            # request returned. Broadcast so every session picks up the
            # now-working shared admin_oauth upstream.
            notify_policy_change(deps)
            asyncio.create_task(_snapshot_started_config_hash())

        def _notify_error(
            error_msg: str, reason: OAuthFailureReason,
        ) -> None:
            del reason  # admin connect flow does not emit Mixpanel events
            if deps.event_bus is not None:
                deps.event_bus.publish(org_id, Event(
                    type="upstream_oauth_error",
                    payload={"upstream_id": upstream_id, "error": error_msg},
                ))

        # Both OAuth modes now key tokens by the connecting admin's
        # real email. The gateway resolves admin_oauth traffic by
        # picking any admin's valid token from the pool (see
        # ``ToolRouter._resolve_admin_pool_user``); per_user_oauth
        # always looks up by caller email.
        result = await connect_and_refresh_tools(
            org_id=org_id,
            upstream=upstream,
            effective_user=admin_email,
            connection_store=deps.connection_store,
            auth_coordinator=deps.auth_coordinator,
            client_manager=runtime.client_manager,
            tool_registry=runtime.tool_registry,
            server_url=deps.server_url,
            on_tokens_acquired=_notify_tokens_acquired,
            on_error=_notify_error,
            # Background catalog refresh fires this after list_tools /
            # list_resources / list_prompts complete. Re-broadcasting
            # ``policy_changed`` flips the dashboard's "Fetching info"
            # indicator off and pulls in the real tool_count.
            on_tools_refreshed=lambda: notify_policy_change(deps),
        )
        if result.connected:
            await deps.connection_store.clear_connection_error(
                org_id, upstream_id,
            )
            await _snapshot_started_config_hash()
            notify_policy_change(deps)
            logger.info(
                "dashboard.api.admin.upstream.connect.success",
                upstream_id=upstream_id,
                org_id=org_id,
            )
        elif result.authorization_url:
            logger.info(
                "dashboard.api.admin.upstream.connect.pending",
                upstream_id=upstream_id,
                org_id=org_id,
            )
        elif result.error:
            await deps.connection_store.set_connection_error(
                org_id, upstream_id, result.error,
            )
            logger.warning(
                "dashboard.api.admin.upstream.connect.failed",
                upstream_id=upstream_id,
                org_id=org_id,
                error=result.error,
            )
        await log_admin_action(
            deps,
            action="connect",
            upstream_id=upstream_id,
            admin_email=admin_email,
            outcome=(
                "success" if result.connected
                else "pending" if result.authorization_url
                else "error"
            ),
            error_message=result.error,
        )
        return ConnectResponse(
            authorization_url=result.authorization_url,
            connected=result.connected,
            error=result.error,
        )

    @router.post("/upstreams/{upstream_id}/disconnect")
    async def disconnect_upstream_admin(
        upstream_id: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")
        logger.info(
            "dashboard.api.admin.upstream.disconnect.requested",
            upstream_id=upstream_id,
            admin_email=admin_email,
            org_id=org_id,
        )
        # Uniform across both OAuth modes: the admin-tab disconnect
        # releases the slot by clearing the slot-owning admin's row —
        # regardless of which admin called the endpoint. This is what
        # makes "B disconnects A, then B connects" work as a take-over
        # for both admin_oauth and per_user_oauth. Non-admin users'
        # personal per_user_oauth rows are never touched here (they're
        # only iterated by ``admin_oauth_owner`` over admin emails).
        # Done before session teardown so the stored token can't be
        # consumed by an in-flight invocation that still resolves the
        # owner.
        cleared_owner: str | None = None
        if (
            upstream.auth.mode in (AuthMode.admin_oauth, AuthMode.per_user_oauth)
            and deps.connection_store is not None
        ):
            cleared_owner = await admin_oauth_owner(
                deps.connection_store, org_id, upstream_id,
                excluding_email=None,
                policy_engine=runtime.policy_engine,
            )
            if cleared_owner is not None:
                await deps.connection_store.delete_user_token(
                    org_id, cleared_owner, upstream_id,
                )
                await runtime.client_manager.disconnect_user_session(
                    upstream_id, cleared_owner,
                )
        try:
            await runtime.client_manager.disconnect_upstream(upstream_id)
        except Exception as e:
            logger.warning(
                "dashboard.api.admin.upstream.disconnect.failed",
                upstream_id=upstream_id,
                org_id=org_id,
                error=str(e),
                exc_info=True,
            )
            await log_admin_action(
                deps,
                action="disconnect",
                upstream_id=upstream_id,
                admin_email=admin_email,
                outcome="error",
                error_message=str(e),
            )
            raise
        if deps.connection_store is not None:
            await deps.connection_store.clear_connection_error(
                org_id, upstream_id,
            )
            # Admin Stop must survive restart — write explicit False
            # rather than removing the marker.
            await deps.connection_store.set_disabled(org_id, upstream_id)
        logger.info(
            "dashboard.api.admin.upstream.disconnect.success",
            upstream_id=upstream_id,
            org_id=org_id,
            cleared_owner=cleared_owner,
        )
        notify_policy_change(deps)
        await log_admin_action(
            deps,
            action="disconnect",
            upstream_id=upstream_id,
            admin_email=admin_email,
            outcome="success",
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
        """Disconnect and reconnect any upstream (all auth modes)."""
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(
            org_id, upstream_id,
        )
        if upstream is None:
            raise HTTPException(404, f"Upstream '{upstream_id}' not found")

        logger.info(
            "dashboard.api.admin.upstream.reconnect.requested",
            upstream_id=upstream_id,
            auth_mode=upstream.auth.mode.value,
            admin_email=admin_email,
            org_id=org_id,
        )

        # Disconnect existing session — don't let failure block the
        # reconnect. ``reset_state=False`` skips the synthetic ``cold``
        # registry transition because we're about to ``mark_warming``
        # in the same handler; without this skip the brief gap between
        # the two events is a window where a refetch returns
        # ``sandbox_state=null`` and clients flicker the button back
        # to "Start" mid-cold-pull.
        try:
            await runtime.client_manager.disconnect_upstream(
                upstream_id, reset_state=False,
            )
        except Exception:
            logger.warning(
                "dashboard.api.admin.upstream.reconnect.disconnect_failed",
                upstream_id=upstream_id,
                org_id=org_id,
                exc_info=True,
            )

        if deps.connection_store is not None:
            # Bistate: removes any explicit-disabled marker so a
            # prior auto-disable doesn't survive the reconnect
            # request. ``is_enabled`` returns True after this; the
            # boot reconciler picks the upstream up next restart.
            await deps.connection_store.set_enabled(org_id, upstream_id)

        if upstream.auth.mode == AuthMode.service_account:
            # Reset stale per-session UX state synchronously so the
            # dashboard sees a clean slate the instant the Start
            # click returns: previous-attempt's ``disconnect_reason``
            # banner and ``Server logs`` panel both come from
            # persistent stores that the in-flight ``transition_to_
            # connecting`` doesn't touch. Without this clear, the
            # user sees the OLD red error + OLD stderr while the
            # button shows "Starting…" — visually confusing during
            # the (now ~500 ms) fail-fast window.
            if deps.connection_store is not None:
                await deps.connection_store.clear_connection_error(
                    org_id, upstream_id,
                )
            runtime.client_manager.log_buffers.get_or_create(
                upstream_id,
            ).clear()

            # Fire-and-forget reconnect. For HTTP upstreams the inner
            # connect is sub-second so the user sees a near-instant
            # ready flip on the next fetch; for remote-stdio sandbox
            # upstreams the runner has to spawn a rootless podman
            # container and ``npx -y`` / ``uvx`` cold-pull the
            # package before MCP ``initialize`` even starts —
            # empirically 30–35s on a t4g.small. Awaiting that
            # synchronously meant the user's tab sat on a spinner
            # and lost the work if they navigated away mid-flight.
            # Instead we hand off to a detached task and rely on the
            # ``sandbox_state_changed`` SSE stream to drive the pill
            # (warming → active → ready) and on
            # ``connection_store.set_connection_error`` +
            # ``mark_failed`` to surface failure state — both of
            # which are reconstructible from server truth on tab
            # switch / reload.
            async def _do_background_reconnect() -> None:
                connection_error: str | None = None
                try:
                    await runtime.client_manager.connect_upstream(upstream)
                except asyncio.CancelledError:
                    # Stop / re-click cancelled us. Don't touch
                    # connection_store or registry — the canceller
                    # owns the post-cancellation state (Stop has
                    # already called mark_disconnected).
                    raise
                except Exception as e:
                    connection_error = str(e) or e.__class__.__name__
                if deps.connection_store is not None:
                    if connection_error:
                        await deps.connection_store.set_connection_error(
                            org_id, upstream_id, connection_error,
                        )
                    else:
                        await deps.connection_store.clear_connection_error(
                            org_id, upstream_id,
                        )
                if connection_error is None:
                    try:
                        await runtime.tool_registry.refresh_upstream(
                            upstream_id,
                        )
                    except Exception:
                        logger.exception(
                            "dashboard.api.admin.upstream.reconnect.refresh_failed",
                            upstream_id=upstream_id,
                            org_id=org_id,
                        )
                    notify_policy_change(deps)
                    logger.info(
                        "dashboard.api.admin.upstream.reconnect.success",
                        upstream_id=upstream_id,
                        org_id=org_id,
                    )
                else:
                    # Re-broadcast policy_changed: ready flips from
                    # True → False on a failed reconnect, and the
                    # listing's pill needs to refetch the new
                    # disconnect_reason from connection_store.
                    notify_policy_change(deps)
                    logger.warning(
                        "dashboard.api.admin.upstream.reconnect.failed",
                        upstream_id=upstream_id,
                        org_id=org_id,
                        error=connection_error,
                    )
                await log_admin_action(
                    deps,
                    action="reconnect",
                    upstream_id=upstream_id,
                    admin_email=admin_email,
                    outcome=(
                        "success" if connection_error is None else "error"
                    ),
                    error_message=connection_error,
                )

            task = asyncio.create_task(_do_background_reconnect())
            runtime.client_manager.register_background_connect_task(
                upstream_id, task,
            )
            logger.info(
                "dashboard.api.admin.upstream.reconnect.scheduled",
                upstream_id=upstream_id,
                org_id=org_id,
            )
            return ConnectResponse(
                outcome="pending",
                upstream_id=upstream_id,
            )
        else:
            # OAuth — delegate to the existing connect flow
            if deps.connection_store is None or deps.auth_coordinator is None:
                raise HTTPException(400, "OAuth is not configured")

            # Same reset-on-Start policy as the service_account branch
            # above. ``connect_and_refresh_tools`` runs synchronously
            # below so the window for showing stale state is shorter,
            # but a non-trivial OAuth handshake (refresh token
            # exchange, retried discovery) can still take a couple of
            # seconds — clear here too so the dashboard stays clean
            # if anything observes the intermediate state.
            await deps.connection_store.clear_connection_error(
                org_id, upstream_id,
            )
            runtime.client_manager.log_buffers.get_or_create(
                upstream_id,
            ).clear()

            async def _snapshot_started_config_hash() -> None:
                # OAuth-mode upstreams compute ``ready`` purely from
                # token existence (no UpstreamState session required),
                # so without an explicit snapshot here the dashboard's
                # dirty banner can NEVER fire — ``started_config_hash``
                # would stay None forever for the new process.
                # Snapshot the current saved config the moment the
                # OAuth flow successfully mints (or refreshes) a token
                # so future config edits cause ``is_dirty=true`` even
                # before any tool call lazily creates an admin session.
                if deps.connection_store is None:
                    return
                hash_value = await runtime.client_manager.compute_runtime_hash(
                    upstream,
                )
                try:
                    await deps.connection_store.set_started_config_hash(
                        org_id, upstream_id, hash_value,
                    )
                except Exception:
                    logger.warning(
                        "dashboard.api.admin.upstream.reconnect."
                        "snapshot_hash_failed",
                        upstream_id=upstream_id,
                        org_id=org_id,
                        exc_info=True,
                    )

            def _on_reconnect_tokens_acquired() -> None:
                # Slow path (tokens arrive via the OAuth callback after
                # this handler has returned): broadcast so every session
                # re-lists the now-reachable upstream's tools.
                notify_policy_change(deps)
                # Snapshot the current saved config as the running
                # baseline. Fire-and-forget — the broadcast above is
                # the user-visible "tokens here, refetch" signal.
                asyncio.create_task(_snapshot_started_config_hash())

            # Reconnect runs as the calling admin — same key shape as
            # the connect path. For ``admin_oauth`` this becomes the
            # new slot owner; for ``per_user_oauth`` it's the admin's
            # personal sign-in.
            result = await connect_and_refresh_tools(
                org_id=org_id,
                upstream=upstream,
                effective_user=admin_email,
                connection_store=deps.connection_store,
                auth_coordinator=deps.auth_coordinator,
                client_manager=runtime.client_manager,
                tool_registry=runtime.tool_registry,
                server_url=deps.server_url,
                on_tokens_acquired=_on_reconnect_tokens_acquired,
                on_tools_refreshed=lambda: notify_policy_change(deps),
            )
            if result.connected:
                await deps.connection_store.clear_connection_error(
                    org_id, upstream_id,
                )
                # Synchronous-success path (silent refresh, or already
                # had a usable token): snapshot the baseline now so
                # the next config edit flips is_dirty.
                await _snapshot_started_config_hash()
                notify_policy_change(deps)
                logger.info(
                    "dashboard.api.admin.upstream.reconnect.success",
                    upstream_id=upstream_id,
                    org_id=org_id,
                )
            elif result.authorization_url:
                logger.info(
                    "dashboard.api.admin.upstream.reconnect.pending",
                    upstream_id=upstream_id,
                    org_id=org_id,
                )
            elif result.error:
                await deps.connection_store.set_connection_error(
                    org_id, upstream_id, result.error,
                )
                logger.warning(
                    "dashboard.api.admin.upstream.reconnect.failed",
                    upstream_id=upstream_id,
                    org_id=org_id,
                    error=result.error,
                )
            await log_admin_action(
                deps,
                action="reconnect",
                upstream_id=upstream_id,
                admin_email=admin_email,
                outcome=(
                    "success" if result.connected
                    else "pending" if result.authorization_url
                    else "error"
                ),
                error_message=result.error,
            )
            return ConnectResponse(
                authorization_url=result.authorization_url,
                connected=result.connected,
                error=result.error,
            )

    return router
