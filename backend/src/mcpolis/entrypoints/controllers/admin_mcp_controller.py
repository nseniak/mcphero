# pyright: reportUnusedFunction=false
# NOTE: no `from __future__ import annotations` — FastMCP tool registration
# uses issubclass() on annotations which breaks with stringified annotations.

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import ToolAnnotations

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import (
    TransportType,
    UpstreamDefinition,
    has_service_account_token,
)
from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.entrypoints.controllers.admin_action_errors import refusal_text
from mcpolis.entrypoints.controllers.admin_tool_calls import (
    install_call_tool_wrapper,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_caller_id,
    current_org_id,
)
from mcpolis.domain.services.admin_actions import (
    AdminActionDeps,
    AdminActionRefused,
    Conflict,
    NoSignInNeeded,
)
from mcpolis.entrypoints.mcp_transport_security import mcp_transport_security
from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
from mcpolis.domain.services.plan_gates import audit_retention_since
from mcpolis.domain.services.plan_policy import PlanLimitExceeded
from mcpolis.domain.services.rate_limit_service import RateLimitService
from mcpolis.domain.services.role_admin_service import RoleAdminService
from mcpolis.domain.services.secret_scanner import (
    hide_secret_args,
    hide_secret_values,
    hide_secrets_in_text,
)
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.domain.services.upstream_admin_service import (
    NewUpstreamRequest,
    UpstreamAdminService,
)
from mcpolis.domain.services.user_admin_service import UserAdminService
from mcpolis.domain.services.upstream_connection_service import (
    OAuthConnectResult,
    refresh_all_with_recovery,
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


# How long ``start_upstream`` waits for a service-account Start, and a
# sign-in for its tool discovery, before answering "still running". The
# action itself keeps going either way. Both stay under the 60 s default
# request timeout of the MCP SDK clients, so the answer reaches them.
_START_WAIT_SECONDS = 45.0
_TOOL_DISCOVERY_WAIT_SECONDS = 40.0


class _ToolDiscovery:
    """The outcome of the tool discovery a sign-in starts."""

    def __init__(self) -> None:
        self._done = asyncio.Event()
        self.error: str | None = None

    def finished(self, error: str | None) -> None:
        self.error = error
        self._done.set()

    async def wait(self) -> bool:
        """Wait for the outcome; False if it is still running."""
        try:
            await asyncio.wait_for(
                self._done.wait(), timeout=_TOOL_DISCOVERY_WAIT_SECONDS,
            )
        except TimeoutError:
            return False
        return True


def _split_list(value: str) -> list[str]:
    """Split a comma-separated tool argument, dropping empty items."""
    return [item.strip() for item in value.split(",") if item.strip()]

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
    *,
    # Required, so no door forgets it: an ``auth_token`` is saved as
    # the secret Variable MCP_AUTH_TOKEN.
    template_var_repo: TemplateVarRepository,
    connection_store: ConnectionStore | None = None,
    auth_coordinator: PendingAuthCoordinator | None = None,
    server_url: str = "http://localhost:8000",
    event_bus: EventStream | None = None,
    revoke_gateway_user: Callable[[str], int] | None = None,
    terminate_gateway_sessions: Callable[[str, str], Awaitable[int]] | None = None,
    allow_stdio_mcp: bool = True,
    org_repo: OrganizationRepository | None = None,
    service_token_service: ServiceTokenService | None = None,
    rate_limits: RateLimitService | None = None,
) -> FastMCP:
    server = FastMCP(
        name="MCP Hero Admin",
        streamable_http_path="/",
        transport_security=mcp_transport_security(),
    )

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

    # The actions the dashboard shares (see ``domain.services.admin_actions``).
    action_deps = AdminActionDeps(
        runtime_manager=runtime_manager,
        policy_store=policy_store,
        audit_repo=audit_repo,
        connection_store=connection_store,
        auth_coordinator=auth_coordinator,
        server_url=server_url,
        event_bus=event_bus,
        org_repo=org_repo,
        allow_stdio_mcp=allow_stdio_mcp,
        revoke_gateway_user=revoke_gateway_user,
        terminate_gateway_sessions=terminate_gateway_sessions,
        service_token_service=service_token_service,
        template_var_repo=template_var_repo,
    )
    user_admin = UserAdminService(action_deps)
    upstream_admin = UpstreamAdminService(action_deps)
    role_admin = RoleAdminService(action_deps)

    async def _sign_in_text(
        mcp_id: str, result: OAuthConnectResult, discovery: _ToolDiscovery,
    ) -> str:
        """Answer a sign-in. Once connected, wait for the tool discovery
        the sign-in started, so the answer can give the tool count."""
        if result.connected:
            if not await discovery.wait():
                return (
                    f"MCP '{mcp_id}' is connected; tool discovery is still "
                    "running. Run list_upstream_tools in a moment."
                )
            if discovery.error is not None:
                return (
                    f"MCP '{mcp_id}' is connected but tool discovery "
                    f"failed: {discovery.error}"
                )
            return (
                f"MCP '{mcp_id}' is connected. "
                f"Discovered {await _tool_count(mcp_id)} tools."
            )
        if result.aborted:
            return (
                f"The start of '{mcp_id}' was interrupted by a stop."
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

    async def _tool_count(mcp_id: str) -> int:
        runtime = await runtime_manager.get(current_org_id.get())
        return len(runtime.tool_registry.get_tools_for_upstreams([mcp_id]))

    async def _status_label(
        org_id: str, runtime: OrgRuntime, upstream_id: str, connected: bool,
    ) -> str:
        if runtime.client_manager.is_starting(upstream_id):
            return "starting"
        if connected:
            return "connected"
        if connection_store is not None:
            error = await connection_store.get_connection_error(
                org_id, upstream_id,
            )
            if error:
                return f"failed: {error}"
            if not await connection_store.is_enabled(org_id, upstream_id):
                return "stopped"
        return "disconnected"

    def _role_access_json(config: SettingsConfig, role_name: str) -> str:
        """Return JSON summary of a role's access config."""
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
            "Get full configuration for a specific upstream MCP. "
            "Credentials are not shown: has_token and "
            "has_client_secret say whether one is set, every header "
            "and env var value reads [hidden] unless it is only "
            "${NAME} Variable references, and a URL part, command "
            "part or argument that may hold a credential reads "
            "[hidden]."
        ),
        annotations=READONLY,
    )
    async def get_upstream(mcp_id: str) -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        upstream = await runtime.config_service.get_upstream(org_id, mcp_id)
        if upstream is None:
            return f"Upstream MCP '{mcp_id}' not found."
        return json.dumps(_upstream_view(upstream), indent=2)

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
            "For service_account: provide auth_token; it is saved as "
            "the secret Variable MCP_AUTH_TOKEN, which the "
            "'Authorization: Bearer' header (streamable_http) or the "
            "MCP_AUTH_TOKEN env var (stdio) refers to. "
            "For OAuth modes: optionally provide scopes "
            "(comma-separated). The MCP SDK discovers OAuth "
            "endpoints automatically from the upstream. "
            "A new MCP starts stopped: start it with start_upstream "
            "(or sign in with connect_upstream for an OAuth MCP)."
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
        try:
            transport_type = TransportType(transport)
        except ValueError:
            return "Error: transport must be 'stdio' or 'streamable_http'."
        stdio = transport_type == TransportType.stdio
        if stdio and not command:
            return "Error: 'command' required for stdio."
        if not stdio and not url:
            return "Error: 'url' required for streamable_http."
        request = NewUpstreamRequest(
            id=mcp_id,
            display_name=display_name,
            command=command if stdio else None,
            args=_split_list(args) if stdio else [],
            url=None if stdio else url,
            auth_mode=auth_mode,
            auth_token=auth_token or None,
            scopes=_split_list(scopes),
        )
        try:
            await upstream_admin.add_upstream(
                current_org_id.get(), request,
                actor=current_caller_id(), source="admin_mcp.add_upstream",
            )
        except (AdminActionRefused, PlanLimitExceeded) as exc:
            return refusal_text(exc)
        # OAuth modes take no static token; say so rather than drop it.
        token_note = (
            " Note: auth_token was not saved: it only applies to "
            "auth_mode 'service_account'."
            if auth_token.strip() and auth_mode != AuthMode.service_account
            else ""
        )
        return (
            f"Upstream MCP '{mcp_id}' added, stopped. Run start_upstream "
            "to start it (connect_upstream signs you in to an OAuth MCP)."
            + token_note
        )

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="remove_upstream",
        description=(
            "Remove an upstream MCP from MCP Hero and disconnect it."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def remove_upstream(mcp_id: str) -> str:
        try:
            await upstream_admin.remove_upstream(
                current_org_id.get(), mcp_id, actor=current_caller_id(),
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
        return f"Upstream MCP '{mcp_id}' removed."

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="disconnect_upstream",
        description=(
            "Stop an upstream MCP without removing it, like the "
            "dashboard's Stop button. The MCP configuration is "
            "preserved and every connection to it closes, the shared "
            "one and each user's own; tool calls are refused until "
            "start_upstream. Saved sign-ins are kept, so start_upstream "
            "brings it back with nobody signing in again. It stays "
            "stopped, even across restarts."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def disconnect_upstream(mcp_id: str) -> str:
        try:
            await upstream_admin.stop_upstream(
                current_org_id.get(), mcp_id, actor=current_caller_id(),
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
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
        org_id = current_org_id.get()
        results = await audit_repo.search(
            org_id,
            user_id=user_id or None,
            mcp_id=mcp_id or None,
            tool=tool or None,
            limit=limit,
            # Same plan retention cap as the dashboard Audit page.
            since_iso=await audit_retention_since(org_repo, org_id),
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
            "Re-discover tools from one or all upstream MCPs. A single "
            "MCP must be running (for an OAuth MCP: an admin is signed "
            "in to it); this never signs anyone in."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def refresh_upstream_tools(mcp_id: str = "") -> str:
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        if not mcp_id:
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
        # The dashboard's Refresh tools, with the same readiness check
        # and audit row. It runs in the background; wait for its
        # discovery to report the tool count.
        try:
            discovered = await upstream_admin.refresh_tools(
                org_id, mcp_id, actor=current_caller_id(),
            )
        except Conflict:
            return (
                f"Error: MCP '{mcp_id}' is not running. Start it with "
                "start_upstream (connect_upstream signs you in to an "
                "OAuth MCP), then refresh."
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
        done, _ = await asyncio.wait(
            {discovered}, timeout=_TOOL_DISCOVERY_WAIT_SECONDS,
        )
        if not done or discovered.cancelled():
            return (
                f"Tool discovery of '{mcp_id}' is still running. Run "
                "list_upstream_tools in a moment."
            )
        error = discovered.result()
        if error is not None:
            return f"Error refreshing '{mcp_id}': {error}"
        return (
            f"Refreshed {await _tool_count(mcp_id)} tools "
            f"from upstream MCP '{mcp_id}'."
        )

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="upstream_status",
        description=(
            "Show each upstream MCP's state: connected, starting, "
            "stopped, failed (with the error), or disconnected."
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
                uid: await _status_label(org_id, runtime, uid, ok)
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
            "discovered and available to users. On a stopped MCP "
            "whose admin sign-in was kept, it reconnects from that "
            "sign-in instead, with no browser. Refused while another "
            "admin is signed in to the MCP (only the dashboard's "
            "Remove sign-in hands it over)."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def connect_upstream(mcp_id: str) -> str:
        discovery = _ToolDiscovery()
        try:
            result = await upstream_admin.connect_upstream(
                current_org_id.get(), mcp_id,
                actor=current_caller_id(),
                on_tools_discovered=discovery.finished,
            )
        except NoSignInNeeded:
            return (
                f"MCP '{mcp_id}' uses service_account auth — no OAuth "
                "connection needed. Run start_upstream to start it."
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
        return await _sign_in_text(mcp_id, result, discovery)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="start_upstream",
        description=(
            "Start an upstream MCP, like the dashboard's Start button. "
            "A stopped MCP serves no tools until it is started. A "
            "service_account MCP that is already running or starting is "
            "left alone; to restart it, run disconnect_upstream, then "
            "start_upstream. For a service_account MCP this waits for the "
            "start to finish; a hosted stdio MCP's first start can take a "
            "minute. For an OAuth MCP stopped with its admin sign-in kept, "
            "it reconnects from that sign-in with no browser; otherwise it "
            "signs you in, and may return an authorization URL to open in "
            "a browser."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def start_upstream(mcp_id: str) -> str:
        org_id = current_org_id.get()
        discovery = _ToolDiscovery()
        try:
            outcome = await upstream_admin.start_upstream(
                org_id, mcp_id,
                actor=current_caller_id(),
                restart=False,
                on_tools_discovered=discovery.finished,
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
        if outcome.already == "starting":
            return (
                f"MCP '{mcp_id}' is already starting. Run upstream_status "
                "in a moment to check."
            )
        if outcome.already == "running":
            return (
                f"MCP '{mcp_id}' is already running. "
                f"{await _tool_count(mcp_id)} tools available."
            )
        if outcome.sign_in is not None:
            return await _sign_in_text(mcp_id, outcome.sign_in, discovery)
        started = outcome.started
        assert started is not None
        # ``asyncio.wait`` never cancels what it waits on, so a client
        # that gives up early leaves the Start running.
        done, _ = await asyncio.wait({started}, timeout=_START_WAIT_SECONDS)
        if not done:
            return (
                f"MCP '{mcp_id}' is still starting. Run upstream_status "
                "in a moment to check."
            )
        result = None if started.cancelled() else started.result()
        if result is not None and result.error is not None:
            return f"Error starting '{mcp_id}': {result.error}"
        if result is None or not result.started:
            return (
                f"The start of '{mcp_id}' was interrupted by a stop or a "
                "newer start."
            )
        if result.discovery_error is not None:
            return (
                f"MCP '{mcp_id}' started, but tool discovery failed: "
                f"{result.discovery_error}"
            )
        return (
            f"MCP '{mcp_id}' started. "
            f"{await _tool_count(mcp_id)} tools available."
        )

    # =====================================================================
    # User Management
    # =====================================================================

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="list_users",
        description=(
            "List all users in MCP Hero with their roles, admin status, "
            "and status: 'active' once they accepted their invitation, "
            "'pending' while only invited."
        ),
        annotations=READONLY,
    )
    async def list_users() -> str:
        views = await user_admin.list_users(current_org_id.get())
        return json.dumps([view.model_dump() for view in views], indent=2)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="add_user",
        description=(
            "Invite a user to MCP Hero by email. "
            "Optionally specify a role (defaults to the default role). "
            "The user stays 'pending', with no access, until they sign "
            "in and accept the invitation."
        ),
        annotations=ADDITIVE,
    )
    async def add_user(email: str, role: str = "") -> str:
        try:
            view = await user_admin.add_user(
                current_org_id.get(), email, role or None,
                actor=current_caller_id(), source="admin_mcp.add_user",
            )
        except (AdminActionRefused, PlanLimitExceeded) as exc:
            return refusal_text(exc)
        return json.dumps(view.model_dump(), indent=2)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="remove_user",
        description=(
            "Remove a user from MCP Hero. "
            "For a member, this also removes them from the organization, "
            "closes their gateway connections to it and disconnects "
            "their upstream sessions. For a pending invitation, it only "
            "deletes the invitation."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def remove_user(email: str) -> str:
        try:
            await user_admin.remove_user(
                current_org_id.get(), email, actor=current_caller_id(),
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
        return f"User '{email}' removed."

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="set_user_role",
        description=(
            "Change a user's role."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def set_user_role(email: str, role: str) -> str:
        try:
            view = await user_admin.set_user_role(
                current_org_id.get(), email, role, actor=current_caller_id(),
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
        return json.dumps(view.model_dump(), indent=2)

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
        summaries = await role_admin.list_roles(current_org_id.get())
        return json.dumps(
            [summary.model_dump() for summary in summaries], indent=2,
        )

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="create_role",
        description=(
            "Create a new role. Optionally copy settings from an "
            "existing role with copy_from."
        ),
        annotations=ADDITIVE,
    )
    async def create_role(name: str, copy_from: str = "") -> str:
        try:
            new_config = await role_admin.create_role(
                current_org_id.get(), name,
                copy_from=copy_from or None,
                actor=current_caller_id(),
                source="admin_mcp.create_role",
            )
        except (AdminActionRefused, PlanLimitExceeded) as exc:
            return refusal_text(exc)
        return _role_access_json(new_config, name)

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="delete_role",
        description=(
            "Delete a role. Fails if any users or service tokens are "
            "assigned to it."
        ),
        annotations=DESTRUCTIVE_IDEMPOTENT,
    )
    async def delete_role(role_name: str) -> str:
        try:
            await role_admin.delete_role(
                current_org_id.get(), role_name, actor=current_caller_id(),
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
        return f"Role '{role_name}' deleted."

    @server.tool(  # pyright: ignore[reportUnusedFunction]
        name="rename_role",
        description=(
            "Rename a role."
        ),
        annotations=ADDITIVE_IDEMPOTENT,
    )
    async def rename_role(role_name: str, new_name: str) -> str:
        try:
            new_config = await role_admin.rename_role(
                current_org_id.get(), role_name, new_name,
                actor=current_caller_id(),
            )
        except AdminActionRefused as exc:
            return refusal_text(exc)
        return _role_access_json(new_config, new_name)

    # =====================================================================
    # Access Policies
    # =====================================================================

    async def _role_edit(
        role_name: str, edit: Awaitable[SettingsConfig],
    ) -> str:
        """Answer a role edit with the role's new settings."""
        try:
            new_config = await edit
        except (AdminActionRefused, PlanLimitExceeded) as exc:
            return refusal_text(exc)
        return _role_access_json(new_config, role_name)

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
        return await _role_edit(role_name, role_admin.set_mcp_access_entry(
            current_org_id.get(), role_name, mcp_id, enabled,
            actor=current_caller_id(),
        ))

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
        return await _role_edit(role_name, role_admin.set_auto_enable_new(
            current_org_id.get(), role_name, auto_enable_new,
        ))

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
        return await _role_edit(role_name, role_admin.set_tool_access_entry(
            current_org_id.get(), role_name, upstream_id, tool_name, enabled,
            actor=current_caller_id(),
        ))

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
        return await _role_edit(role_name, role_admin.remove_tool_access_entry(
            current_org_id.get(), role_name, upstream_id, tool_name,
        ))

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
        parsed: bool | None
        if fallback_enabled.lower() == "null":
            parsed = None
        elif fallback_enabled.lower() == "true":
            parsed = True
        elif fallback_enabled.lower() == "false":
            parsed = False
        else:
            return "Error: fallback_enabled must be 'true', 'false', or 'null'."
        return await _role_edit(role_name, role_admin.set_tool_fallback_enabled(
            current_org_id.get(), role_name, upstream_id, parsed,
        ))

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
        return await _role_edit(role_name, role_admin.set_category_default(
            current_org_id.get(), role_name, upstream_id, annotation, enabled,
        ))

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
        return await _role_edit(role_name, role_admin.remove_category_default(
            current_org_id.get(), role_name, upstream_id, annotation,
        ))

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
        return await _role_edit(role_name, role_admin.set_argument_constraint(
            current_org_id.get(), role_name, upstream_id, tool_name, arg_name,
            pattern=pattern,
            mode=mode,
            actor=current_caller_id(),
            source="admin_mcp.set_role_argument_constraint",
        ))

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
        return await _role_edit(
            role_name,
            role_admin.remove_argument_constraint(
                current_org_id.get(), role_name, upstream_id, tool_name,
                arg_name,
            ),
        )

    install_call_tool_wrapper(server, rate_limits)

    return server


def _upstream_view(upstream: UpstreamDefinition) -> dict[str, Any]:
    """An upstream's configuration as ``get_upstream`` shows it.

    An AI client reads it, so no credential may be in it: it says
    whether a service-account token or an OAuth client secret is set,
    never its value. Every header and env value is hidden unless it is
    made only of Variable references (``${NAME}``); in the URL, the
    command and its arguments, what may be a credential is hidden (user
    info, a password-like parameter or flag, an env var set on the
    command line, a header argument, anything shaped like a key). Names
    and references stay, so the client can still tell how it's wired.
    """
    data = upstream.model_dump(mode="json", exclude_none=True)
    auth: dict[str, Any] = data["auth"]
    auth.pop("client_secret", None)
    auth["has_token"] = has_service_account_token(upstream)
    auth["has_client_secret"] = upstream.auth.client_secret is not None
    if upstream.http is not None:
        http: dict[str, Any] = data["http"]
        http["url"] = hide_secrets_in_text(upstream.http.url)
        http["headers"] = hide_secret_values(upstream.http.headers)
    if upstream.stdio is not None:
        stdio: dict[str, Any] = data["stdio"]
        stdio["command"] = hide_secrets_in_text(upstream.stdio.command)
        stdio["args"] = hide_secret_args(upstream.stdio.args)
        stdio["env"] = hide_secret_values(upstream.stdio.env)
    return data
