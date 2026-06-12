"""Dashboard route dependencies + shared helpers.

``DashboardDeps`` mirrors the 16-parameter signature of
:func:`mcpolis.entrypoints.routes.dashboard_api.create_dashboard_api_router`
exactly. Per-concern route files (``upstream_admin.py``, ``roles.py``,
…) will accept a single ``deps`` argument; the top-level factory
constructs the dataclass once and passes it to every per-concern
``create_X_router(deps)`` call.

The five helpers historically defined as module-level functions in
``dashboard_api.py`` plus the two factory-closure helpers
(``_log_admin_action`` / ``_notify_policy_change``) live here too, so
every router file can import the same implementations rather than
duplicating them. ``_log_admin_action`` and ``_notify_policy_change``
take ``deps`` as their first argument — what was a closure capture in
the old factory becomes an explicit parameter.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.events import Event
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.plan_gates import (
    resolve_plan_limits as _resolve_plan_limits_via_repo,
)
from mcpolis.domain.services.plan_policy import PlanLimits
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id

if TYPE_CHECKING:
    from mcpolis.domain.services.org_runtime import OrgRuntime
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
    terminate_gateway_sessions: Callable[[str, str], int] | None
    event_bus: EventStream | None
    list_admin_mcp_tools: Callable[[], Awaitable[Any]] | None
    allow_stdio_mcp: bool
    org_repo: OrganizationRepository | None
    is_cloud_mode: bool
    template_var_repo: TemplateVarRepository
    sandbox_file_repo: SandboxFileRepository | None = None
    service_token_service: ServiceTokenService | None = None


def sse_encode(text: str) -> str:
    """Encode text for SSE data field (newlines become separate data: lines)."""
    return json.dumps(text)


async def resolve_plan_limits(deps: "DashboardDeps", org_id: str) -> PlanLimits:
    """Look up the active org's plan limits.

    Thin adapter over
    :func:`mcpolis.domain.services.plan_gates.resolve_plan_limits` —
    keeps the existing dashboard call signature
    (``deps`` + ``org_id``) while routing through the shared helper.
    """
    return await _resolve_plan_limits_via_repo(deps.org_repo, org_id)


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


# Sentinel used as the ``updated_at`` ordering key for legacy token
# rows that predate the field. They sort before any timestamped row,
# which means a freshly stored token always wins over a legacy one —
# the desired behaviour for slot-owner display.
_LEGACY_TOKEN_TS = datetime.min.replace(tzinfo=UTC)


async def admin_oauth_owner(
    connection_store: ConnectionStore,
    org_id: str,
    upstream_id: str,
    excluding_email: str | None,
    *,
    policy_engine: PolicyEngine,
) -> str | None:
    """Return the email of the admin who currently owns the
    ``admin_oauth`` slot for *upstream_id*, or ``None`` if no admin
    holds the slot. Optionally excludes *excluding_email* (used by
    the connect handler to ignore the caller's own re-connect of an
    upstream they already own — that should succeed, not 409).

    Used by the admin-tab take-over conflict path (single-slot
    semantics): caller only needs to know "is *any* other admin
    holding the slot?" — first hit suffices.
    """
    admin_emails = policy_engine.get_admin_emails()
    for email in admin_emails:
        if email == excluding_email:
            continue
        token = await connection_store.get_user_token(
            org_id, email, upstream_id,
        )
        if token is not None:
            return email
    return None


async def resolve_upstream_readiness(
    upstream: UpstreamDefinition,
    org_id: str,
    connection_store: ConnectionStore | None,
    runtime: "OrgRuntime",
) -> tuple[bool, str | None]:
    """Compute ``(ready, slot_owner)`` for an upstream.

    The single source of truth for both the admin tab and /my-tools.
    Definitions:

    - ``service_account``: Ready iff the shared session is live;
      ``slot_owner`` is always ``None``.
    - ``admin_oauth`` and ``per_user_oauth``: Ready iff at least one
      admin has a stored token row. ``slot_owner`` is that admin's
      email. When several admins hold rows for the same
      ``per_user_oauth`` upstream, the most-recently updated one
      wins — fixes the displayed owner against a stable rule (see
      Risk 1 of internal/plans/upstream-readiness-uniform-oauth.md).

    Non-admin users' rows do NOT count towards readiness. The
    invariant "Ready ⇔ admin authenticated" is what the admin tab
    UI relies on, and it's load-bearing for non-admin /my-tools
    flows: if no admin is signed in, the upstream is Not Ready
    even when non-admins have personal rows.
    """
    if upstream.auth.mode == AuthMode.service_account:
        return runtime.client_manager.is_connected(upstream.id), None
    if connection_store is None:
        return False, None
    admin_emails = runtime.policy_engine.get_admin_emails()
    best: tuple[str, datetime] | None = None
    for email in admin_emails:
        token = await connection_store.get_user_token(
            org_id, email, upstream.id,
        )
        if token is None:
            continue
        ts = token.updated_at if token.updated_at is not None else _LEGACY_TOKEN_TS
        if best is None or ts > best[1]:
            best = (email, ts)
    if best is None:
        return False, None
    return True, best[0]


async def log_admin_action(
    deps: DashboardDeps,
    *,
    action: str,
    upstream_id: str,
    admin_email: str,
    outcome: str,
    error_message: str | None = None,
) -> None:
    """Append one admin-action row to the audit log.

    Was a closure inside ``create_dashboard_api_router``; pulled out
    so per-concern router files can import it instead of re-binding
    a closure. Takes ``deps`` for the audit-repo handle.
    """
    entry = AuditEntry(
        timestamp=datetime.now(UTC).isoformat(),
        action=action,
        user_id=admin_email,
        upstream_id=upstream_id,
        outcome=outcome,
        error_message=error_message,
    )
    await deps.audit_repo.log(current_org_id.get(), entry)


def notify_policy_change(
    deps: DashboardDeps,
    *,
    role: str | None = None,
    user: str | None = None,
) -> None:
    """Publish a ``policy_changed`` event so gateway sessions get the
    new tool lists / role memberships.

    Was a closure inside ``create_dashboard_api_router``; pulled out
    so per-concern router files can import it. No-op when no event
    bus is wired (some tests / dev configs).
    """
    if deps.event_bus is None:
        return
    payload: dict[str, object] = {}
    if role is not None:
        payload["role"] = role
    if user is not None:
        payload["user"] = user
    deps.event_bus.publish(
        current_org_id.get(),
        Event(type="policy_changed", payload=payload),
    )
