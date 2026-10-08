"""Write audit rows.

``write_audit_entry`` is the one place a row reaches the audit store.
It never raises: by the time a row is written, the thing it records has
already happened (a teammate removed, a tool call answered), and a down
audit store must not turn that into an error for the person who acted,
or stop the rest of the action halfway. A failed write is logged at
ERROR, which Sentry captures.

``record_action`` builds the row for an account or admin action. Every
action row goes through it: the shared admin actions (both admin doors
reach them), the operator routes, and ``admin_actions.log_admin_action``.
Tool calls build their own rows in ``tool_router``.

``acting_as_operator`` says, for the current request, that an MCP Hero
operator is acting rather than the org's own admin. The request's access
check sets it: the operator routes always, the dashboard only when it
admitted the caller through the operator list. ``record_action`` reads
it, so every row an operator causes carries the operator tag, including
rows written by background work the request started.
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

import structlog

from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.ports.audit_repository import AuditRepository

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

OPERATOR: Literal["operator"] = "operator"

acting_as_operator: ContextVar[bool] = ContextVar(
    "acting_as_operator", default=False,
)

# Action names written by this module's callers. The org's admin actions
# (both admin doors write the same rows): ``target_user_id`` names the
# teammate, ``upstream_id`` the MCP, ``detail`` the role or token.
MEMBER_INVITED = "member_invited"
MEMBER_ROLE_CHANGED = "member_role_changed"
MEMBER_REMOVED = "member_removed"
# An org admin signed a member out of the gateway (the Gateway page's
# Disconnect, or the same request through the API).
GATEWAY_SIGN_IN_REVOKED = "gateway_sign_in_revoked"
UPSTREAM_ADDED = "upstream_added"
UPSTREAM_REMOVED = "upstream_removed"
REFRESH_TOOLS = "refresh_tools"
ROLE_CREATED = "role_created"
ROLE_RENAMED = "role_renamed"
ROLE_DELETED = "role_deleted"
SERVICE_TOKEN_CREATED = "service_token_created"
SERVICE_TOKEN_REVOKED = "service_token_revoked"
OPERATOR_SIGN_OUT_EVERYWHERE = "operator_sign_out_everywhere"
OPERATOR_CLEAR_SIGN_IN = "operator_clear_sign_in"
OPERATOR_PLAN_CHANGE = "operator_plan_change"


async def write_audit_entry(
    audit_repo: AuditRepository, org_id: str, entry: AuditEntry,
) -> None:
    try:
        await audit_repo.log(org_id, entry)
    except Exception:
        # ``except Exception`` lets a cancellation through. The fields
        # name the row, never its contents beyond what a row may hold.
        logger.exception(
            "audit.write_failed",
            org_id=org_id,
            action=entry.action,
            user_id=entry.user_id,
            upstream_id=entry.upstream_id,
            tool=entry.tool,
        )


async def record_action(
    audit_repo: AuditRepository,
    org_id: str,
    *,
    action: str,
    actor: str,
    outcome: str = "success",
    upstream_id: str = "",
    target_user_id: str | None = None,
    detail: str | None = None,
    error_message: str | None = None,
) -> None:
    await write_audit_entry(audit_repo, org_id, AuditEntry(
        timestamp=datetime.now(UTC).isoformat(),
        action=action,
        org_id=org_id,
        user_id=actor,
        upstream_id=upstream_id,
        outcome=outcome,
        error_message=error_message,
        actor_role=OPERATOR if acting_as_operator.get() else None,
        target_user_id=target_user_id,
        detail=detail,
    ))
