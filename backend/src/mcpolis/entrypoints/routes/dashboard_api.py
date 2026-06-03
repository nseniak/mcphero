"""Dashboard REST API — thin top-level router factory.

Per-concern route modules live under
``mcpolis.entrypoints.routes.dashboard``. This file is a stable entry
point: it constructs ``DashboardDeps`` from the historical
parameter list and composes one ``APIRouter`` per concern via
``include_router``. URL prefixes, dependencies, and tags are owned by
the per-concern files so routes never accidentally drift from their
admin auth gate when they move.

``StartupStatusResponse`` is re-exported from
``mcpolis.entrypoints.routes.dashboard.meta`` so ``app.py``'s existing
``from .dashboard_api import StartupStatusResponse`` import path keeps
working.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard.audit import create_audit_router
from mcpolis.entrypoints.routes.dashboard.auth_connect import (
    create_auth_connect_router,
)
from mcpolis.entrypoints.routes.dashboard.config import create_config_router
from mcpolis.entrypoints.routes.dashboard.events import create_events_router
from mcpolis.entrypoints.routes.dashboard.gateway_admin import (
    create_gateway_admin_router,
)
from mcpolis.entrypoints.routes.dashboard.meta import (
    StartupStatusResponse,
    create_meta_router,
)
from mcpolis.entrypoints.routes.dashboard.sandbox_files import (
    create_sandbox_files_router,
)
from mcpolis.entrypoints.routes.dashboard.template_vars import create_template_vars_router
from mcpolis.entrypoints.routes.dashboard.roles import create_roles_router
from mcpolis.entrypoints.routes.dashboard.tools import create_tools_router
from mcpolis.entrypoints.routes.dashboard.upstream_admin import (
    create_upstream_admin_router,
)
from mcpolis.entrypoints.routes.dashboard.upstream_user import (
    create_upstream_user_router,
)
from mcpolis.entrypoints.routes.dashboard.users_admin import (
    create_users_admin_router,
)

__all__ = [
    "DashboardDeps",
    "StartupStatusResponse",
    "create_dashboard_api_router",
]


def create_dashboard_api_router(
    runtime_manager: OrgRuntimeManager,
    policy_store: ConfigRepository,
    audit_repo: AuditRepository,
    connection_store: ConnectionStore | None,
    auth_coordinator: PendingAuthCoordinator | None,
    server_url: str,
    get_current_user: Callable[..., str],
    require_admin: Callable[..., str],
    get_startup_status: Callable[[], StartupStatusResponse] | None = None,
    get_gateway_connected_users: Callable[[], list[str]] | None = None,
    revoke_gateway_user: Callable[[str], int] | None = None,
    terminate_gateway_sessions: Callable[[str, str], int] | None = None,
    event_bus: EventStream | None = None,
    list_admin_mcp_tools: Callable[[], Any] | None = None,
    allow_stdio_mcp: bool = True,
    org_repo: OrganizationRepository | None = None,
    is_cloud_mode: bool = False,
    template_var_repo: TemplateVarRepository | None = None,
    sandbox_file_repo: SandboxFileRepository | None = None,
    gateway_url: str | None = None,
) -> APIRouter:
    if template_var_repo is None:
        raise RuntimeError(
            "create_dashboard_api_router requires template_var_repo "
            "(per-MCP env-var store)"
        )
    deps = DashboardDeps(
        runtime_manager=runtime_manager,
        policy_store=policy_store,
        audit_repo=audit_repo,
        connection_store=connection_store,
        auth_coordinator=auth_coordinator,
        server_url=server_url,
        gateway_url=gateway_url if gateway_url is not None else server_url,
        get_current_user=get_current_user,
        require_admin=require_admin,
        get_startup_status=get_startup_status,
        get_gateway_connected_users=get_gateway_connected_users,
        revoke_gateway_user=revoke_gateway_user,
        terminate_gateway_sessions=terminate_gateway_sessions,
        event_bus=event_bus,
        list_admin_mcp_tools=list_admin_mcp_tools,
        allow_stdio_mcp=allow_stdio_mcp,
        org_repo=org_repo,
        is_cloud_mode=is_cloud_mode,
        template_var_repo=template_var_repo,
        sandbox_file_repo=sandbox_file_repo,
    )

    combined = APIRouter()
    # Admin-prefixed concerns first; ordering doesn't affect routing
    # (no path overlaps between concerns) but keeping like with like
    # makes openapi.json easier to scan.
    combined.include_router(create_meta_router(deps))
    combined.include_router(create_upstream_admin_router(deps))
    combined.include_router(create_template_vars_router(deps))
    combined.include_router(create_sandbox_files_router(deps))
    combined.include_router(create_tools_router(deps))
    combined.include_router(create_audit_router(deps))
    combined.include_router(create_roles_router(deps))
    combined.include_router(create_users_admin_router(deps))
    combined.include_router(create_gateway_admin_router(deps))
    # User-facing + auth + config + events.
    combined.include_router(create_upstream_user_router(deps))
    combined.include_router(create_auth_connect_router(deps))
    combined.include_router(create_config_router(deps))
    combined.include_router(create_events_router(deps))
    return combined
