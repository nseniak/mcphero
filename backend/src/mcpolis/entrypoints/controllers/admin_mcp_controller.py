# pyright: reportUnusedFunction=false
# NOTE: no `from __future__ import annotations` — FastMCP tool registration
# uses issubclass() on annotations which breaks with stringified annotations.

import json
import re
from collections.abc import Callable
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.events import Event
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import ArgumentConstraint, UserDefinition
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_user_id,
)
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.plan_gates import (
    assert_argument_constraints_allowed,
    assert_custom_role_capacity,
    assert_http_upstream_capacity,
    assert_sandbox_combo_allowed,
    assert_seat_capacity,
    assert_stdio_upstream_capacity,
    resolve_plan,
)
from mcpolis.domain.services.plan_policy import PlanLimitExceeded
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.domain.services.sandbox_service import (
    ResourcesUnsupported,
    SandboxResources,
)
from mcpolis.domain.services.settings_resolver import resolve_settings
from mcpolis.domain.services.upstream_connection_service import (
    SessionUnavailable,
    acquire_and_refresh_with_recovery,
    initiate_oauth_connection,
    recovery_effective_user,
    refresh_all_with_recovery,
)
from mcpolis.domain.services.url_safety import (
    UnsafeUpstreamUrl,
    validate_upstream_url,
)

_DEFAULT_ADMIN_INSTRUCTIONS = (
    "Administration interface for MCP Hero. Tools here manage the gateway "
    "itself — upstream MCPs, roles, users, permissions, audit. Use these "
    "to configure what other users see through the gateway; do not use "
    "them to perform end-user tasks."
)


def _admin_instructions_for_org(
    org_id: str, display_name: str | None,
) -> str:
    if org_id == DEFAULT_ORG_ID or not display_name:
        return _DEFAULT_ADMIN_INSTRUCTIONS
    return (
        f"Administration interface for MCP Hero, scoped to {display_name}. "
        f"Tools here manage {display_name}'s gateway configuration — "
        f"upstream MCPs, roles, users, permissions, audit. Use these to "
        f"configure what {display_name}'s users see through the gateway; "
        f"do not use them to perform end-user tasks."
    )

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# --- Annotation presets ---

READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
ADDITIVE = ToolAnnotations(destructiveHint=False, idempotentHint=False)
ADDITIVE_IDEMPOTENT = ToolAnnotations(destructiveHint=False, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True, idempotentHint=False)
DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(destructiveHint=True, idempotentHint=True)


def create_admin_mcp_server(
    runtime_manager: OrgRuntimeManager,
    audit_repo: AuditRepository,
    policy_store: ConfigRepository,
    connection_store: ConnectionStore | None = None,
    auth_coordinator: PendingAuthCoordinator | None = None,
    server_url: str = "http://localhost:8000",
    event_bus: EventStream | None = None,
    revoke_gateway_user: Callable[[str], int] | None = None,
    terminate_gateway_sessions: Callable[[str, str], int] | None = None,
    allow_stdio_mcp: bool = True,
    org_repo: OrganizationRepository | None = None,
    service_token_service: ServiceTokenService | None = None,
) -> FastMCP:
    server = FastMCP(name="MCP Hero Admin", streamable_http_path="/")

    # Inject org-scoped ``instructions`` into each new session's
    # ``initialize`` response. Mirrors the gateway controller; we reach
    # the underlying low-level Server (``_mcp_server``) because FastMCP
    # wraps it but doesn't re-expose ``create_initialization_options``.
    _low_level_server = server._mcp_server  # pyright: ignore[reportPrivateUsage]
    _original_init_options = _low_level_server.create_initialization_options

    def _org_scoped_init_options(
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        opts = _original_init_options(
            notification_options=notification_options,
            experimental_capabilities=experimental_capabilities,
        )
        try:
            org_id = current_org_id.get()
        except LookupError:
            org_id = DEFAULT_ORG_ID
        display_name = runtime_manager.get_display_name(org_id)
        # See gateway_controller for rationale — keep org-scoped names
        # so multiple instances / orgs don't look identical in clients.
        server_name = (
            f"MCP Hero Admin — {display_name}"
            if display_name and org_id != DEFAULT_ORG_ID
            else "MCP Hero Admin"
        )
        return opts.model_copy(
            update={
                "server_name": server_name,
                "instructions": _admin_instructions_for_org(
                    org_id, display_name,
                ),
            },
        )

    _low_level_server.create_initialization_options = _org_scoped_init_options

    # --- Helpers ---

    def _notify_policy_change(
        *, role: str | None = None, user: str | None = None,
    ) -> None:
        """Publish a policy_changed event so gateway sessions get updated tool lists."""
        if event_bus is None:
            return
        payload: dict[str, object] = {}
        if role is not None:
            payload["role"] = role
        if user is not None:
            payload["user"] = user
        event_bus.publish(current_org_id.get(), Event(type="policy_changed", payload=payload))

    async def _ensure_oauth_session(mcp_id: str) -> None:
        """If an OAuth upstream has stored tokens but no session, connect.

        Auto-establishes a session under the calling admin's email so
        ``refresh_upstream_tools`` can list tools immediately after
        ``connect_upstream``. Skips if any per-user session already
        exists for the upstream — discovery only needs one live MCP
        session, regardless of which user owns it.
        """
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        if connection_store is None or auth_coordinator is None:
            return
        upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
        if upstream is None:
            return
        if upstream.auth.mode == AuthMode.service_account:
            return
        # Any per-user session for the upstream is enough for
        # discovery — discovery is identity-agnostic.
        if runtime.client_manager.any_user_session_for_upstream(
            mcp_id,
        ) is not None:
            return
        caller_email = current_user_id.get()
        result = await initiate_oauth_connection(
            org_id=org_id,
            upstream=upstream,
            effective_user=caller_email,
            connection_store=connection_store,
            auth_coordinator=auth_coordinator,
            client_manager=runtime.client_manager,
            server_url=server_url,
        )
        if not result.connected:
            logger.debug(
                "admin.mcp.oauth.auto_connect.failed",
                upstream_id=mcp_id,
                org_id=org_id,
            )

    def _role_access_json(policy_engine: PolicyEngine, role_name: str) -> str:
        """Return JSON summary of a role's access config."""
        config = policy_engine.config
        role_def = config.roles.get(role_name)
        if role_def is None:
            return json.dumps({"error": f"Role '{role_name}' not found."})
        s = role_def.settings
        return json.dumps({
            "name": role_name,
            "is_admin": role_def.is_admin,
            "is_default": role_def.is_default,
            "mcp_access": s.mcp_access.model_dump(mode="json"),
            "tool_access": {
                k: v.model_dump(mode="json") for k, v in s.tool_access.items()
            },
            "argument_constraints": s.argument_constraints,
        }, indent=2)

    # =====================================================================
    # Upstream MCPs
    # =====================================================================

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="list_upstreams",
        description=(
            "List all upstream MCPs in MCP Hero with their connection "
            "status. Upstream MCPs are the MCP servers that "
            "MCP Hero aggregates and proxies to end-users."
        ),
        annotations=READONLY,
    )
    async def list_upstreams() -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        upstreams = await runtime.config_service.list_upstreams(org_id)
        status = runtime.config_service.connection_status()
        result: list[dict[str, Any]] = []
        for u in upstreams:
            result.append({
                "id": u.id,
                "display_name": u.display_name,
                "transport": u.transport.value,
                "auth_mode": u.auth.mode.value,
                "connected": status.get(u.id, False),
            })
        return json.dumps(result, indent=2)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="get_upstream",
        description=(
            "Get full configuration for a specific upstream MCP."
        ),
        annotations=READONLY,
    )
    async def get_upstream(mcp_id: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
        if upstream is None:
            return f"Upstream MCP '{mcp_id}' not found."
        return upstream.model_dump_json(indent=2, exclude_none=True)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="add_upstream",
        description=(
            "Add a new upstream MCP to MCP Hero. "
            "transport must be 'stdio' or 'streamable_http'. "
            "For stdio: provide command and optionally args "
            "(comma-separated). "
            "For streamable_http: provide url. "
            "auth_mode: 'service_account' (default), 'admin_oauth', "
            "or 'per_user_oauth'. "
            "For service_account: provide auth_token. "
            "For OAuth modes: optionally provide scopes "
            "(comma-separated). The MCP SDK discovers OAuth "
            "endpoints automatically from the upstream."
        ),
        annotations=ADDITIVE,
    )
    async def add_upstream(
        mcp_id: str,
        display_name: str,
        transport: str,
        command: str = "",
        args: str = "",
        url: str = "",
        auth_mode: str = "service_account",
        auth_token: str = "",
        scopes: str = "",
    ) -> str:
        org_id = current_org_id.get()
        actor_email = current_user_id.get()
        runtime = await runtime_manager.get(org_id)
        transport_type = TransportType(transport)
        stdio_config = None
        http_config = None
        if transport_type == TransportType.stdio:
            if not allow_stdio_mcp:
                return "Error: Stdio MCP servers are disabled."
            if not command:
                return "Error: 'command' required for stdio."
            arg_list = [
                a.strip() for a in args.split(",") if a.strip()
            ] if args else []
            stdio_config = StdioTransportConfig(
                command=command, args=arg_list
            )
        else:
            if not url:
                return "Error: 'url' required for streamable_http."
            try:
                validate_upstream_url(url)
            except UnsafeUpstreamUrl as exc:
                return (
                    f"Error: UNSAFE_UPSTREAM_URL — {exc.reason}. "
                    "Upstream MCPs cannot target private/loopback ranges."
                )
            http_config = HttpTransportConfig(url=url)

        # Plan gate: count existing upstreams by transport before any
        # work happens. Sandbox-combo gate runs after the provider
        # validator below so off-grid values still get the more
        # specific error.
        try:
            plan = await resolve_plan(org_repo, org_id)
            existing = await runtime.config_service.list_upstreams(org_id)
            if transport_type == TransportType.stdio:
                current_stdio = sum(
                    1 for u in existing
                    if u.transport == TransportType.stdio
                )
                assert_stdio_upstream_capacity(
                    plan, current_stdio,
                    source="admin_mcp.add_upstream",
                    org_id=org_id,
                    actor_email=actor_email,
                )
            else:
                current_http = sum(
                    1 for u in existing
                    if u.transport == TransportType.streamable_http
                )
                assert_http_upstream_capacity(
                    plan, current_http,
                    source="admin_mcp.add_upstream",
                    org_id=org_id,
                    actor_email=actor_email,
                )

            # Sandbox-combo plan gate for stdio. The admin MCP tool
            # signature doesn't yet expose CPU / memory knobs, so the
            # combo is always the model defaults
            # (1 vCPU / 1024 MB) which Free supports — guard the
            # default explicitly anyway so a future signature change
            # doesn't silently bypass the gate. Provider-grid
            # validation runs first to keep the order matching the
            # dashboard.
            if transport_type == TransportType.stdio and stdio_config is not None:
                try:
                    caps = await runtime.client_manager.get_active_capabilities()
                    services = runtime.client_manager._sandbox_services  # type: ignore[reportPrivateUsage]
                    services[caps.provider].validate_resources(
                        SandboxResources(
                            cpu_vcpus=stdio_config.cpu_vcpus,
                            memory_mb=stdio_config.memory_mb,
                            disk_gb=stdio_config.disk_gb,
                            pids_limit=stdio_config.pids_limit,
                        ),
                    )
                except ResourcesUnsupported as exc:
                    return f"Error: {exc}"
                assert_sandbox_combo_allowed(
                    plan,
                    stdio_config.cpu_vcpus,
                    stdio_config.memory_mb,
                    source="admin_mcp.add_upstream",
                    org_id=org_id,
                    actor_email=actor_email,
                )
        except PlanLimitExceeded as e:
            return f"Error: {e.message}"

        mode = AuthMode(auth_mode)
        auth = UpstreamAuthConfig(
            mode=mode,
            token=auth_token if auth_token else None,
            scopes=(
                [s.strip() for s in scopes.split(",") if s.strip()]
                if scopes else []
            ),
        )
        try:
            upstream = UpstreamDefinition(
                id=mcp_id,
                display_name=display_name,
                transport=transport_type,
                stdio=stdio_config,
                http=http_config,
                auth=auth,
            )
        except ValidationError as e:
            return f"Error: {e}"
        try:
            await runtime.config_service.add_upstream(org_id, upstream)
            new_config = await policy_store.create_mcp_access(org_id, mcp_id)
            runtime.policy_engine.reload(new_config)
            return (
                f"Upstream MCP '{mcp_id}' added (disconnected). "
                "Use connect_upstream or reconnect from the "
                "dashboard to connect."
            )
        except ValueError as e:
            return f"Error: {e}"

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="remove_upstream",
        description=(
            "Remove an upstream MCP from MCP Hero and disconnect it."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def remove_upstream(mcp_id: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            await runtime.config_service.remove_upstream(org_id, mcp_id)
            return f"Upstream MCP '{mcp_id}' removed."
        except ValueError as e:
            return f"Error: {e}"

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="disconnect_upstream",
        description=(
            "Disconnect an upstream MCP without removing it. "
            "The MCP configuration is preserved but the connection "
            "is closed."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def disconnect_upstream(mcp_id: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
        if upstream is None:
            return f"Upstream MCP '{mcp_id}' not found."
        await runtime.client_manager.disconnect_upstream(mcp_id)
        if connection_store is not None:
            await connection_store.clear_connection_error(org_id, mcp_id)
            # Admin Stop must survive restart — write explicit False.
            await connection_store.set_disabled(org_id, mcp_id)
        _notify_policy_change()
        return f"Upstream MCP '{mcp_id}' disconnected."

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="list_upstream_tools",
        description=(
            "List all discovered tools for a specific upstream MCP."
        ),
        annotations=READONLY,
    )
    async def list_upstream_tools(mcp_id: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
        if upstream is None:
            return f"Upstream MCP '{mcp_id}' not found."
        tools = runtime.tool_registry.get_tools_for_upstreams([mcp_id])
        result: list[dict[str, Any]] = []
        for t in tools:
            entry: dict[str, Any] = {
                "name": t.original_name,
                "prefixed_name": t.prefixed_name,
                "description": t.description,
            }
            if t.annotations:
                ann = {
                    k: v for k, v in t.annotations.model_dump().items()
                    if v is not None
                }
                if ann:
                    entry["annotations"] = ann
            result.append(entry)
        return json.dumps(result, indent=2)

    # =====================================================================
    # Tool Customization
    # =====================================================================

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="list_default_arguments",
        description=(
            "Show default_arguments for an upstream MCP in MCP Hero."
        ),
        annotations=READONLY,
    )
    async def list_default_arguments(mcp_id: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
        if upstream is None:
            return f"Upstream MCP '{mcp_id}' not found."
        return json.dumps(
            {"default_arguments": upstream.default_arguments}, indent=2
        )

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_default_arguments",
        description=(
            "Set static arguments that are silently injected into "
            "every call to a tool on an upstream MCP. "
            "arguments_json should be a JSON object, "
            "e.g. '{\"org\": \"acme\"}'."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_default_arguments(
        mcp_id: str, tool_name: str, arguments_json: str
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            arguments: dict[str, Any] = json.loads(arguments_json)
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON — {e}"
        try:
            await runtime.config_service.set_default_arguments(org_id,
                mcp_id, tool_name, arguments
            )
            return (
                f"Default arguments for "
                f"'{mcp_id}/{tool_name}' updated."
            )
        except ValueError as e:
            return f"Error: {e}"

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="remove_default_arguments",
        description=(
            "Remove default_arguments for a tool on an upstream MCP."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def remove_default_arguments(mcp_id: str, tool_name: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            await runtime.config_service.remove_default_arguments(org_id,
                mcp_id, tool_name
            )
            return f"Default arguments for '{mcp_id}/{tool_name}' removed."
        except ValueError as e:
            return f"Error: {e}"

    # =====================================================================
    # Audit Log
    # =====================================================================

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="search_audit_log",
        description=(
            "Search MCP Hero audit log entries. "
            "All filters are optional. "
            "Returns the most recent entries matching the filters."
        ),
        annotations=READONLY,
    )
    async def search_audit_log(
        user_id: str = "",
        mcp_id: str = "",
        tool: str = "",
        limit: int = 20,
    ) -> str:
        results = await audit_repo.search(
            current_org_id.get(),
            user_id=user_id or None,
            mcp_id=mcp_id or None,
            tool=tool or None,
            limit=limit,
        )
        if not results:
            return "No matching audit log entries found."
        return json.dumps(results, indent=2)

    # =====================================================================
    # Operations
    # =====================================================================

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="refresh_upstream_tools",
        description=(
            "Re-discover tools from one or all upstream MCPs."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def refresh_upstream_tools(mcp_id: str = "") -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        if mcp_id:
            upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
            if upstream is None:
                return f"Upstream MCP '{mcp_id}' not found."
            # Establish an OAuth session under the caller (no-op for
            # service_account or when one already exists), then refresh
            # WITH transport-stall recovery so an E2B post-reattach stall
            # heals + retries instead of persisting a partial catalogue
            # (R6). This BLOCKS the admin's MCP client (worst case
            # ~2×(establish+timeout)); unlike the dashboard's non-blocking
            # refresh (c54c0b3) that is intended — this tool's contract is
            # to return the discovered tool COUNT synchronously, which a
            # backgrounded refresh can't do.
            await _ensure_oauth_session(mcp_id)
            try:
                tools = await acquire_and_refresh_with_recovery(
                    org_id=org_id,
                    upstream=upstream,
                    effective_user=recovery_effective_user(
                        runtime.client_manager, upstream,
                    ),
                    connection_store=connection_store,
                    client_manager=runtime.client_manager,
                    tool_registry=runtime.tool_registry,
                    server_url=server_url,
                )
                return (
                    f"Refreshed {len(tools)} tools "
                    f"from upstream MCP '{mcp_id}'."
                )
            except SessionUnavailable as e:
                return (
                    f"Error refreshing '{mcp_id}': could not reattach "
                    f"session ({e.reason})."
                )
            except Exception as e:
                return f"Error refreshing '{mcp_id}': {e}"
        else:
            await refresh_all_with_recovery(
                org_id=org_id,
                connection_store=connection_store,
                client_manager=runtime.client_manager,
                tool_registry=runtime.tool_registry,
                server_url=server_url,
            )
            total = len(runtime.tool_registry.get_all_tools())
            return (
                f"Refreshed all upstream MCPs. Total: {total} tools."
            )

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="upstream_status",
        description=(
            "Show which upstream MCPs are connected or disconnected."
        ),
        annotations=READONLY,
    )
    async def upstream_status() -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        status = runtime.config_service.connection_status()
        if not status:
            return "No upstream MCPs configured in MCP Hero."
        return json.dumps(
            {
                uid: "connected" if ok else "disconnected"
                for uid, ok in status.items()
            },
            indent=2,
        )

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="check_upstream_auth_status",
        description=(
            "Check the auth status of an upstream MCP. "
            "For OAuth modes, shows whether the upstream is "
            "connected (tokens obtained)."
        ),
        annotations=READONLY,
    )
    async def check_upstream_auth_status(mcp_id: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
        if upstream is None:
            return f"Upstream MCP '{mcp_id}' not found."
        connected = runtime.config_service.connection_status().get(
            mcp_id, False
        )
        return json.dumps({
            "mcp_id": mcp_id,
            "auth_mode": upstream.auth.mode.value,
            "connected": connected,
        }, indent=2)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="connect_upstream",
        description=(
            "Connect to an OAuth-protected upstream MCP. "
            "Returns an authorization URL that you must open in "
            "a browser to complete the OAuth flow. "
            "After authenticating, the MCP's tools will be "
            "discovered and available to users."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def connect_upstream(mcp_id: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        if connection_store is None or auth_coordinator is None:
            return "Error: OAuth is not configured."
        upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
        if upstream is None:
            return f"Upstream MCP '{mcp_id}' not found."
        if upstream.auth.mode == AuthMode.service_account:
            return (
                f"MCP '{mcp_id}' uses service_account auth — "
                "no OAuth connection needed."
            )

        caller_email = current_user_id.get()
        # Single-slot admin_oauth: another admin already owning the slot
        # must be explicitly disconnected before this admin can take
        # over. Surface the conflict so the caller can decide whether
        # to invoke disconnect first.
        if upstream.auth.mode == AuthMode.admin_oauth:
            for email in runtime.policy_engine.get_admin_emails():
                if email == caller_email:
                    continue
                token = await connection_store.get_user_token(
                    org_id, email, mcp_id,
                )
                if token is not None:
                    return (
                        f"'{email}' is already connected to '{mcp_id}'. "
                        "Disconnect first to take over."
                    )
        result = await initiate_oauth_connection(
            org_id=org_id,
            upstream=upstream,
            effective_user=caller_email,
            connection_store=connection_store,
            auth_coordinator=auth_coordinator,
            client_manager=runtime.client_manager,
            server_url=server_url,
        )

        if result.connected:
            try:
                tools = await runtime.tool_registry.refresh_upstream(mcp_id)
                return (
                    f"MCP '{mcp_id}' is already connected. "
                    f"Discovered {len(tools)} tools."
                )
            except Exception as e:
                return (
                    f"MCP '{mcp_id}' is connected but "
                    f"tool discovery failed: {e}"
                )

        if result.authorization_url:
            return (
                f"Please open this URL in your browser to "
                f"authorize '{mcp_id}':\n\n"
                f"{result.authorization_url}\n\n"
                "After authorizing, run "
                f"refresh_upstream_tools(mcp_id=\"{mcp_id}\") "
                "to discover tools."
            )

        return f"Error: {result.error or 'connection failed'}"

    # =====================================================================
    # User Management
    # =====================================================================

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="list_users",
        description=(
            "List all users in MCP Hero with their roles and admin status."
        ),
        annotations=READONLY,
    )
    async def list_users() -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        results: list[dict[str, Any]] = []
        for email, user_def in config.users.items():
            resolved = resolve_settings(config, email)
            results.append({
                "email": email,
                "role": user_def.role,
                "is_admin": resolved.is_admin,
            })
        return json.dumps(results, indent=2)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="add_user",
        description=(
            "Add a user to MCP Hero by email. "
            "Optionally specify a role (defaults to the default role)."
        ),
        annotations=ADDITIVE,
    )
    async def add_user(email: str, role: str = "") -> str:
        org_id = current_org_id.get()
        actor_email = current_user_id.get()
        runtime = await runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        if email in config.users:
            return f"Error: user '{email}' already exists."
        try:
            plan = await resolve_plan(org_repo, org_id)
            assert_seat_capacity(
                plan, len(config.users),
                source="admin_mcp.add_user",
                org_id=org_id,
                actor_email=actor_email,
            )
        except PlanLimitExceeded as e:
            return f"Error: {e.message}"
        if not role:
            default_role = runtime.policy_engine.get_default_role()
            if default_role is None:
                return "Error: no default role configured."
            role = default_role
        if role not in config.roles:
            return f"Error: role '{role}' not found."
        user_def = UserDefinition(role=role)
        new_config = await policy_store.set_user(org_id, email, user_def)
        runtime.policy_engine.reload(new_config)
        resolved = resolve_settings(new_config, email)
        return json.dumps({
            "email": email,
            "role": role,
            "is_admin": resolved.is_admin,
        }, indent=2)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="remove_user",
        description=(
            "Remove a user from MCP Hero. "
            "This also revokes their gateway tokens and disconnects "
            "their upstream sessions."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def remove_user(email: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.remove_user(org_id, email)
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(user=email)
        if terminate_gateway_sessions is not None:
            terminate_gateway_sessions(org_id, email)
        if revoke_gateway_user is not None:
            revoke_gateway_user(email)
        await runtime.client_manager.disconnect_all_user_sessions(email)
        if connection_store is not None:
            # Purge the whole per-user key family (tokens + client_info +
            # oauth_metadata + counters), so a re-invite re-registers
            # cleanly instead of reusing a dead DCR client_info.
            await connection_store.delete_all_for_user(org_id, email)
        return f"User '{email}' removed."

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_user_role",
        description=(
            "Change a user's role."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_user_role(email: str, role: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.set_user_role(org_id, email, role)
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(user=email)
        resolved = resolve_settings(new_config, email)
        return json.dumps({
            "email": email,
            "role": role,
            "is_admin": resolved.is_admin,
        }, indent=2)

    # =====================================================================
    # Role Management
    # =====================================================================

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="list_roles",
        description=(
            "List all roles in MCP Hero with admin/default flags "
            "and user counts."
        ),
        annotations=READONLY,
    )
    async def list_roles() -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        user_counts: dict[str, int] = {}
        for user_def in config.users.values():
            user_counts[user_def.role] = user_counts.get(user_def.role, 0) + 1
        token_counts: dict[str, int] = {}
        if service_token_service is not None:
            token_counts = await service_token_service.count_by_role(org_id)
        results = [
            {
                "name": name,
                "is_admin": role_def.is_admin,
                "is_default": role_def.is_default,
                "user_count": user_counts.get(name, 0),
                "service_token_count": token_counts.get(name, 0),
            }
            for name, role_def in config.roles.items()
        ]
        return json.dumps(results, indent=2)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="create_role",
        description=(
            "Create a new role. Optionally copy settings from an "
            "existing role with copy_from."
        ),
        annotations=ADDITIVE,
    )
    async def create_role(name: str, copy_from: str = "") -> str:
        org_id = current_org_id.get()
        actor_email = current_user_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            plan = await resolve_plan(org_repo, org_id)
            current_custom = sum(
                1 for r in runtime.policy_engine.config.roles.values()
                if not r.is_admin and not r.is_default
            )
            assert_custom_role_capacity(
                plan, current_custom,
                source="admin_mcp.create_role",
                org_id=org_id,
                actor_email=actor_email,
            )
        except PlanLimitExceeded as e:
            return f"Error: {e.message}"
        try:
            new_config = await policy_store.create_role(
                org_id, name, copy_from=copy_from or None,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        return _role_access_json(runtime.policy_engine, name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="delete_role",
        description=(
            "Delete a role. Fails if any users or service tokens are "
            "assigned to it."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def delete_role(role_name: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        # Service-token guard — mirrors the dashboard route: the
        # policy store only knows config.users.
        if service_token_service is not None:
            token_counts = await service_token_service.count_by_role(org_id)
            in_use = token_counts.get(role_name, 0)
            if in_use:
                return (
                    f"Error: Cannot delete role '{role_name}': {in_use} "
                    f"service token(s) assigned"
                )
        try:
            new_config = await policy_store.delete_role(org_id, role_name)
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return f"Role '{role_name}' deleted."

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="rename_role",
        description=(
            "Rename a role."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def rename_role(role_name: str, new_name: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.rename_role(org_id, role_name, new_name)
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, new_name)

    # =====================================================================
    # Access Policies
    # =====================================================================

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_role_mcp_access",
        description=(
            "Set whether a role can access a specific MCP. "
            "enabled=true grants access, enabled=false denies it."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_role_mcp_access(
        role_name: str, mcp_id: str, enabled: bool,
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.set_role_mcp_access_entry(
                org_id, role_name, mcp_id, enabled,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_role_auto_enable_new",
        description=(
            "Set the auto-enable-new flag for a role. "
            "When true, newly added MCPs are auto-enabled for this role. "
            "When false, MCPs must be explicitly enabled."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_role_auto_enable_new(
        role_name: str, auto_enable_new: bool,
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.set_role_auto_enable_new(
                org_id, role_name, auto_enable_new,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_role_tool_access",
        description=(
            "Allow or deny a specific tool for a role on an MCP. "
            "enabled=true allows the tool, enabled=false denies it."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_role_tool_access(
        role_name: str, upstream_id: str, tool_name: str, enabled: bool,
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.set_role_tool_access_entry(
                org_id, role_name, upstream_id, tool_name, enabled,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="remove_role_tool_access",
        description=(
            "Remove a per-tool override for a role on an MCP, "
            "reverting the tool to the default access policy."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def remove_role_tool_access(
        role_name: str, upstream_id: str, tool_name: str,
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.remove_role_tool_access_entry(
                org_id, role_name, upstream_id, tool_name,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_role_tool_fallback_enabled",
        description=(
            "Set the fallback-enabled flag for tools on a specific "
            "MCP within a role. Controls what happens to tools that "
            "have no explicit override. "
            "Use 'true', 'false', or 'null' (to remove the setting)."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_role_tool_fallback_enabled(
        role_name: str, upstream_id: str, fallback_enabled: str,
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        parsed: bool | None
        if fallback_enabled.lower() == "null":
            parsed = None
        elif fallback_enabled.lower() == "true":
            parsed = True
        elif fallback_enabled.lower() == "false":
            parsed = False
        else:
            return "Error: fallback_enabled must be 'true', 'false', or 'null'."
        try:
            new_config = await policy_store.set_role_tool_fallback_enabled(
                org_id, role_name, upstream_id, parsed,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_role_category_default",
        description=(
            "Set an annotation-based tool access policy for a role "
            "on an MCP. annotation is one of: 'destructiveHint', "
            "'readOnlyHint', 'idempotentHint', 'openWorldHint'. "
            "enabled=true allows tools with this annotation, "
            "enabled=false denies them."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_role_category_default(
        role_name: str, upstream_id: str, annotation: str, enabled: bool,
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.set_role_tool_category_default(
                org_id, role_name, upstream_id, annotation, enabled,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="remove_role_category_default",
        description=(
            "Remove an annotation-based tool access policy for a "
            "role on an MCP."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def remove_role_category_default(
        role_name: str, upstream_id: str, annotation: str,
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.remove_role_tool_category_default(
                org_id, role_name, upstream_id, annotation,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_role_argument_constraint",
        description=(
            "Set a regex constraint on a tool argument for a role. "
            "Mode 'allow': argument must match the pattern. "
            "Mode 'forbid': argument must NOT match (case-insensitive)."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_role_argument_constraint(
        role_name: str, upstream_id: str, tool_name: str,
        arg_name: str, pattern: str, mode: str = "allow",
    ) -> str:
        org_id = current_org_id.get()
        actor_email = current_user_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            plan = await resolve_plan(org_repo, org_id)
            assert_argument_constraints_allowed(
                plan,
                source="admin_mcp.set_role_argument_constraint",
                org_id=org_id,
                actor_email=actor_email,
            )
        except PlanLimitExceeded as e:
            return f"Error: {e.message}"
        try:
            re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regex pattern — {e}"
        if mode not in ("allow", "forbid"):
            return f"Error: invalid mode '{mode}', must be 'allow' or 'forbid'"
        constraint = ArgumentConstraint(pattern=pattern, mode=mode)
        try:
            new_config = await policy_store.set_role_argument_constraint(
                org_id, role_name, upstream_id, tool_name, arg_name, constraint,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="remove_role_argument_constraint",
        description=(
            "Remove a regex constraint on a tool argument for a role."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def remove_role_argument_constraint(
        role_name: str, upstream_id: str, tool_name: str, arg_name: str,
    ) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        try:
            new_config = await policy_store.remove_role_argument_constraint(
                org_id, role_name, upstream_id, tool_name, arg_name,
            )
        except ValueError as e:
            return f"Error: {e}"
        runtime.policy_engine.reload(new_config)
        _notify_policy_change(role=role_name)
        return _role_access_json(runtime.policy_engine, role_name)

    return server
