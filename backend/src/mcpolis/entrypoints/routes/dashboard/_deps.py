"""Dashboard route dependencies + shared helpers.

``DashboardDeps`` mirrors the 16-parameter signature of
:func:`mcpolis.entrypoints.routes.dashboard_api.create_dashboard_api_router`
exactly. Per-concern route files (``upstream_admin.py``, ``roles.py``,
…) will accept a single ``deps`` argument; the top-level factory
constructs the dataclass once and passes it to every per-concern
``create_X_router(deps)`` call.

The helpers historically defined as module-level functions in
``dashboard_api.py`` plus the factory-closure helper
``notify_policy_change`` live here too, so every router file can import
the same implementations rather than duplicating them.
``notify_policy_change`` takes ``deps`` as its first argument and
delegates to the domain helper the Admin MCP uses too
(``domain.services.admin_actions``). Upstream readiness lives with the
shared upstream actions (``resolve_upstream_readiness``).
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.email_allowlist import EmailAllowlist
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.services.admin_actions import publish_policy_changed
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.role_admin_service import RoleAdminService
from mcpolis.domain.services.upstream_admin_service import UpstreamAdminService
from mcpolis.domain.services.user_admin_service import UserAdminService
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id

if TYPE_CHECKING:
    from mcpolis.domain.services.service_token_service import (
        ServiceTokenService,
    )

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DashboardDeps:
    """All dependencies the dashboard routers need.

    Mirrors the historical
    ``create_dashboard_api_router(...)`` parameter list exactly — the
    field names and types match what ``app.py`` already passes. New
    per-concern router files accept a single ``deps: DashboardDeps``
    rather than re-declaring the 16 parameters.
    """

    runtime_manager: OrgRuntimeManager
    policy_store: ConfigRepository
    audit_repo: AuditRepository
    connection_store: ConnectionStore | None
    auth_coordinator: PendingAuthCoordinator | None
    server_url: str
    # Public base URL the gateway MCP is exposed at. Equal to
    # ``server_url`` for single-origin deploys; differs only when the
    # operator runs the gateway on its own subdomain (see
    # ``effective_gateway_url`` in ``entrypoints.config``).
    gateway_url: str
    get_current_user: Callable[..., str]
    require_admin: Callable[..., str]
    get_startup_status: Callable[[], Any] | None
    get_gateway_connected_users: Callable[[], list[str]] | None
    revoke_gateway_user: Callable[[str], int] | None
    terminate_gateway_sessions: Callable[[str, str], Awaitable[int]] | None
    event_bus: EventStream | None
    list_admin_mcp_tools: Callable[[], Awaitable[Any]] | None
    allow_stdio_mcp: bool
    org_repo: OrganizationRepository | None
    is_cloud_mode: bool
    template_var_repo: TemplateVarRepository
    # The actions both admin doors share (see ``domain.services.admin_actions``).
    user_admin: UserAdminService
    upstream_admin: UpstreamAdminService
    role_admin: RoleAdminService
    sandbox_file_repo: SandboxFileRepository | None = None
    service_token_service: ServiceTokenService | None = None
    # MCP Hero operators (``MCPOLIS_SUPERADMIN_EMAILS``): they may browse
    # any org without being a member of it.
    superadmin_emails: EmailAllowlist = field(default_factory=EmailAllowlist)
    # Signed in, whatever org (``DashboardAuth.get_session_user``): for
    # the routes an invited person uses before they are a member. None
    # falls back to ``get_current_user``.
    get_session_user: Callable[..., str] | None = None


def sse_encode(text: str) -> str:
    """Encode text for SSE data field (newlines become separate data: lines)."""
    return json.dumps(text)


def compose_user_mcp_url(
    *, server_url: str, is_cloud_mode: bool, org_slug: str,
) -> str:
    """Build the user-facing MCP URL shown on Connect / Gateway pages.

    Cloud mode: ``{server_url}/mcp/{slug}`` — slug-scoped. Each org
    sees its own URL and the gateway only surfaces that org's tools.
    The bare ``/mcp`` URL also works under the hood (merge mode across
    every org the user belongs to) but is intentionally not surfaced
    in the product yet.

    Standalone mode: ``{server_url}/mcp`` — single-org install, no
    slug to disambiguate.
    """
    base = server_url.rstrip("/")
    if is_cloud_mode and org_slug:
        return f"{base}/mcp/{org_slug}"
    return f"{base}/mcp"


def notify_policy_change(
    deps: DashboardDeps,
    *,
    role: str | None = None,
    user: str | None = None,
) -> None:
    """Publish ``policy_changed`` for the current org so gateway
    sessions get the new tool lists / role memberships."""
    publish_policy_changed(
        deps.event_bus, current_org_id.get(), role=role, user=user,
    )
