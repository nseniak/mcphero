"""What the admin actions share: their dependencies, refusals and
side-channel notices.

An org admin reaches the same actions through two doors: the dashboard
(``/api/admin/*``) and the Admin MCP (``/admin-mcp``). Each action lives
once, in :mod:`user_admin_service`, :mod:`upstream_admin_service` or
:mod:`role_admin_service`. A door only maps its request onto the action,
and the outcome or the refusal onto its own answer shape. When each door
carried its own copy, the copies drifted: the Admin MCP left membership
rows behind, never marked a new upstream stopped, never freed an admin
sign-in slot, and never audited a tool refresh.

An action that changes something runs to its end once started, whatever
cancels the request meanwhile (see :mod:`cancel_shield`), and audits
what it did.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.events import Event
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.services.audit_actions import record_action
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.service_token_service import ServiceTokenService


@dataclass(frozen=True)
class AdminActionDeps:
    """Everything the shared admin actions need. Both doors build one
    from the dependencies ``app.py`` already hands them."""

    runtime_manager: OrgRuntimeManager
    policy_store: ConfigRepository
    audit_repo: AuditRepository
    connection_store: ConnectionStore | None
    auth_coordinator: PendingAuthCoordinator | None
    server_url: str
    event_bus: EventStream | None
    org_repo: OrganizationRepository | None
    allow_stdio_mcp: bool
    revoke_gateway_user: Callable[[str], int] | None = None
    terminate_gateway_sessions: Callable[[str, str], Awaitable[int]] | None = None
    # Where an add saves its Variables: the dashboard form's, and the
    # secret Variable ``MCP_AUTH_TOKEN`` an ``auth_token`` becomes.
    template_var_repo: TemplateVarRepository | None = None
    # Service tokens hold a role by name: role renames move them, role
    # deletes count them.
    service_token_service: ServiceTokenService | None = None


class AdminActionRefused(Exception):
    """An admin action refused to run. The message says why, in words
    the admin can act on; each door picks its own answer shape."""


class NotFound(AdminActionRefused):
    """The user or upstream the action names does not exist."""


class AlreadyExists(AdminActionRefused):
    """The action would create something that already exists."""


class InvalidRequest(AdminActionRefused):
    """The request itself is wrong: a bad value or a missing field."""


class Conflict(AdminActionRefused):
    """The request is valid, but the org's current state forbids it."""


class SignInSlotTaken(Conflict):
    """Another admin holds the upstream's single admin sign-in slot.
    Stop keeps it; only Remove sign-in (the dashboard) frees it."""

    def __init__(self, owner: str) -> None:
        super().__init__(f"'{owner}' is already signed in to this MCP.")
        self.owner = owner


class SignInNeedsMembership(Conflict):
    """Someone who is not a member of the org (an MCP Hero operator
    browsing it) asked to sign in to one of its MCPs. Only members'
    sign-ins land: the upstream's callback refuses anyone else, so the
    consent page would be for nothing."""

    def __init__(self) -> None:
        super().__init__(
            "Only members of this organization can sign in to its MCPs, "
            "and you are not one. Ask one of its admins to connect it.",
        )


class SignInChanged(Conflict):
    """Remove sign-in named an admin whose sign-in is no longer the one
    the upstream shows: nothing was removed."""

    def __init__(self, expected: str, shown: str) -> None:
        super().__init__(
            f"The sign-in shown is now {shown}'s, not {expected}'s. "
            "Nothing was removed; reload to see it.",
        )
        self.expected = expected
        self.shown = shown


class NoSignInNeeded(InvalidRequest):
    """Sign-in was asked for an upstream that uses a service account."""

    def __init__(self) -> None:
        super().__init__("This MCP uses service_account auth")


class UnsupportedSandboxSize(InvalidRequest):
    """The sandbox provider cannot run the requested CPU / RAM / disk.
    ``field`` names the control the dashboard form flags."""

    def __init__(self, message: str, *, field: str, value: object) -> None:
        super().__init__(message)
        self.field = field
        self.value = value


class UnsafeServerUrl(InvalidRequest):
    """The upstream URL targets a private or loopback range."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "This URL targets a private/loopback range and cannot be "
            "used as an upstream MCP.",
        )
        self.reason = reason


def publish_policy_changed(
    event_bus: EventStream | None,
    org_id: str,
    *,
    role: str | None = None,
    user: str | None = None,
) -> None:
    """Publish ``policy_changed`` so gateway sessions re-list their
    tools and dashboards refetch. No-op without an event bus."""
    if event_bus is None:
        return
    payload: dict[str, object] = {}
    if role is not None:
        payload["role"] = role
    if user is not None:
        payload["user"] = user
    event_bus.publish(org_id, Event(type="policy_changed", payload=payload))


async def log_admin_action(
    audit_repo: AuditRepository,
    org_id: str,
    *,
    action: str,
    upstream_id: str,
    admin_email: str,
    outcome: str,
    error_message: str | None = None,
    target_user_id: str | None = None,
) -> None:
    """Append one admin-action row to the org's audit log, through the
    one action-row writer (see :mod:`audit_actions`). ``target_user_id``
    is who the action was done to, e.g. whose sign-in Remove sign-in
    deleted."""
    await record_action(
        audit_repo, org_id,
        action=action,
        actor=admin_email,
        upstream_id=upstream_id,
        outcome=outcome,
        error_message=error_message,
        target_user_id=target_user_id,
    )
