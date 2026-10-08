"""How each admin door answers a refused admin action.

The shared actions raise :class:`AdminActionRefused` subclasses (and
:class:`PlanLimitExceeded` for plan caps). The dashboard turns them into
HTTP answers through an app-level exception handler; the Admin MCP turns
them into ``"Error: ..."`` tool texts.
"""
from __future__ import annotations

from mcpolis.domain.services.admin_actions import (
    AdminActionRefused,
    AlreadyExists,
    Conflict,
    NotFound,
    UnsafeServerUrl,
    UnsupportedSandboxSize,
)
from mcpolis.domain.services.plan_policy import PlanLimitExceeded


def refusal_status(exc: AdminActionRefused) -> int:
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, AlreadyExists | Conflict):
        return 409
    return 400


def refusal_detail(exc: AdminActionRefused) -> str | dict[str, str]:
    """The ``detail`` of the dashboard's error body. Sandbox-size and
    unsafe-URL refusals keep the structured shape the add / edit forms
    read to flag the offending field."""
    if isinstance(exc, UnsupportedSandboxSize):
        return {"message": str(exc), "field": exc.field, "value": str(exc.value)}
    if isinstance(exc, UnsafeServerUrl):
        return {
            "code": "UNSAFE_UPSTREAM_URL",
            "message": str(exc),
            "reason": exc.reason,
        }
    return str(exc)


def refusal_text(exc: AdminActionRefused | PlanLimitExceeded) -> str:
    """The Admin MCP tool answer for a refused action."""
    if isinstance(exc, PlanLimitExceeded):
        return f"Error: {exc.message}"
    if isinstance(exc, UnsafeServerUrl):
        return (
            f"Error: UNSAFE_UPSTREAM_URL — {exc.reason}. "
            "Upstream MCPs cannot target private/loopback ranges."
        )
    return f"Error: {exc}"
