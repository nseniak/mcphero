from __future__ import annotations

from dataclasses import dataclass

from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    SettingsConfig,
    ToolAccessConfig,
)


@dataclass(frozen=True)
class ResolvedSettings:
    mcp_access: McpAccessConfig
    tool_access: dict[str, ToolAccessConfig]
    default_arguments: dict[str, dict[str, dict[str, object]]]
    argument_constraints: dict[str, dict[str, ArgumentConstraint]]
    role_name: str
    is_admin: bool


_EMPTY = ResolvedSettings(
    mcp_access=McpAccessConfig(),
    tool_access={},
    default_arguments={},
    argument_constraints={},
    role_name="",
    is_admin=False,
)


def resolve_settings(config: SettingsConfig, email: str) -> ResolvedSettings:
    """Resolve effective settings for a user by looking up their role directly."""
    user_def = config.users.get(email)
    if user_def is None:
        return _EMPTY
    return resolve_settings_for_role(config, user_def.role)


def resolve_settings_for_role(
    config: SettingsConfig, role_name: str
) -> ResolvedSettings:
    """Resolve effective settings for a role known a priori.

    Used for identities whose role is established at the auth
    boundary (service tokens) instead of via ``config.users``. An
    unknown role — e.g. deleted after a token was minted — fails
    closed with the same ``_EMPTY`` sentinel a role-less user gets.
    """
    role_def = config.roles.get(role_name)
    if role_def is None:
        return _EMPTY

    s = role_def.settings
    return ResolvedSettings(
        mcp_access=s.mcp_access,
        tool_access=s.tool_access,
        default_arguments=s.default_arguments,
        argument_constraints=s.argument_constraints,
        role_name=role_name,
        is_admin=role_def.is_admin,
    )


class LastAdminError(ValueError):
    """Raised when a write would leave an org with no admin.

    A ValueError subclass so the existing ``except ValueError`` arms in
    the callers keep working, but a distinct type so the routes can map
    it to 409 instead of their generic 400 / 404.
    """


# Shared wording for the last-admin refusal, so the dashboard and the
# admin MCP tools explain the same rule the same way. The message says
# what to do next, because the caller is often the sole admin trying to
# hand the org over and needs to know the order of operations.
LAST_ADMIN_REMOVE_ERROR = (
    "This is the only admin in the organization. Removing them would "
    "leave nobody able to manage it, and no admin could undo it. Give "
    "another member an admin role first, then remove this one."
)
LAST_ADMIN_DEMOTE_ERROR = (
    "This is the only admin in the organization. Changing their role "
    "would leave nobody able to manage it, and no admin could undo it. "
    "Give another member an admin role first, then change this one."
)


def admin_emails(
    config: SettingsConfig, *, eligible: set[str] | None = None
) -> set[str]:
    """Every user whose role currently grants admin rights.

    A user's role name is resolved through ``config.roles``: a role
    that was deleted after the user was assigned to it grants nothing,
    which is the same fail-closed rule ``resolve_settings_for_role``
    applies.

    ``eligible`` narrows the count to a given set of addresses — the
    callers pass the members who have actually signed in. An invited
    address that never signs in can never administer anything, so
    counting it would let a mistyped invitation stand in for a real
    admin and brick the org exactly as before.
    """
    return {
        email
        for email, user_def in config.users.items()
        if (eligible is None or email in eligible)
        and (role := config.roles.get(user_def.role)) is not None
        and role.is_admin
    }


def would_remove_last_admin(
    config: SettingsConfig,
    email: str,
    *,
    new_role: str | None = None,
    eligible: set[str] | None = None,
) -> bool:
    """Would removing ``email`` — or moving them to ``new_role`` —
    leave the org with nobody who can administer it?

    ``new_role=None`` means removal. Otherwise the check is a
    demotion: it only bites when the target role is not an admin role.

    This is the org's one unrecoverable state. Every other
    member-management mistake can be undone by an admin; losing the
    last one cannot, because the screens and the admin MCP tools are
    both gated on ``require_admin``. Only a service operator can
    repair it, by hand, in the database.

    Called by both mutation paths — the dashboard routes and the admin
    MCP tools — so the rule cannot hold on one door and not the other.
    """
    admins = admin_emails(config, eligible=eligible)
    if email not in admins:
        # Removing or re-roling a non-admin never changes the count.
        return False
    if new_role is not None:
        target = config.roles.get(new_role)
        if target is not None and target.is_admin:
            # Admin to admin: still an admin afterwards.
            return False
    return admins == {email}


def assert_keeps_an_admin(
    config: SettingsConfig, email: str, *, new_role: str | None = None
) -> None:
    """Raise ``LastAdminError`` if this write would zero the admins.

    Called by the config stores INSIDE their write lock, so the check
    and the write are one atomic step. A route-level pre-check cannot
    be: two parallel calls each read "two admins", each pass, and the
    org ends with none. That race is reachable in cloud mode, where an
    assistant issuing two tool calls in one turn is an ordinary event.

    The routes still pre-check, purely to return a friendlier status
    and message than a store exception carries.
    """
    if would_remove_last_admin(config, email, new_role=new_role):
        raise LastAdminError(
            LAST_ADMIN_DEMOTE_ERROR if new_role is not None
            else LAST_ADMIN_REMOVE_ERROR
        )
