from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from mcpolis.domain.model.email_address import (
    email_key,
    every_spelling,
    find_address,
)
from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    SettingsConfig,
    ToolAccessConfig,
    UserDefinition,
)
from mcpolis.domain.ports.config_repository import UserAlreadyExistsError


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

# What an identity with no role resolves to: no MCPs, no tools, no
# admin rights. A pending invitation resolves to it too.
NO_ROLE = _EMPTY


def resolve_settings(config: SettingsConfig, email: str) -> ResolvedSettings:
    """Resolve effective settings for a user by looking up their role
    directly. The address is found ignoring letter case."""
    key = find_address(config.users, email)
    if key is None:
        return _EMPTY
    return resolve_settings_for_role(config, config.users[key].role)


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
    config: SettingsConfig, *, eligible: Collection[str] | None = None
) -> set[str]:
    """Every user whose role currently grants admin rights.

    A user's role name is resolved through ``config.roles``: a role
    that was deleted after the user was assigned to it grants nothing,
    which is the same fail-closed rule ``resolve_settings_for_role``
    applies.

    ``eligible`` narrows the count to a given set of addresses — the
    callers pass the members who accepted their invitation. A pending
    invitation can never administer anything, so counting it would let
    a mistyped invitation stand in for a real admin and brick the org
    exactly as before. Addresses compare ignoring letter case.
    """
    eligible_keys = (
        {email_key(email) for email in eligible}
        if eligible is not None else None
    )
    return {
        email
        for email, user_def in config.users.items()
        if (eligible_keys is None or email_key(email) in eligible_keys)
        and (role := config.roles.get(user_def.role)) is not None
        and role.is_admin
    }


def would_remove_last_admin(
    config: SettingsConfig,
    email: str,
    *,
    new_role: str | None = None,
    eligible: Collection[str] | None = None,
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

    Admins are counted as people, not as entries: ``email`` is found
    ignoring letter case, so another spelling of the sole admin is
    refused too, and a sole admin whose address the org's users hold
    under two spellings (saved before letter case was ignored) is one
    admin, whom a removal or a role change takes out under both.
    """
    admins = {email_key(admin) for admin in admin_emails(config, eligible=eligible)}
    person = email_key(email)
    if person not in admins:
        # Removing or re-roling a non-admin never changes the count.
        return False
    if new_role is not None:
        target = config.roles.get(new_role)
        if target is not None and target.is_admin:
            # Admin to admin: still an admin afterwards.
            return False
    return admins == {person}


def assert_is_new_user(config: SettingsConfig, email: str) -> None:
    """Raise ``UserAlreadyExistsError`` if ``email`` is already a user,
    under any letter case.

    Called by the config stores' ``set_user`` inside their atomic
    write step. Every caller means "add": an overwrite would change an
    existing user's role with no last-admin check, and two parallel
    adds of one address would silently keep only the last role.
    """
    existing = find_address(config.users, email)
    if existing is not None:
        raise UserAlreadyExistsError(f"User '{existing}' already exists")


def add_user_to_config(
    config: SettingsConfig, email: str, user: UserDefinition,
) -> None:
    """Add ``email`` to ``config.users`` with ``user``'s role.

    Shared by the config stores' ``set_user`` and ``add_first_user``,
    which call it inside their atomic write step: a caller's earlier
    check can be stale if the role was renamed or deleted since, or the
    address added meanwhile. Raises ``ValueError`` when the role is not
    a role of the org, ``UserAlreadyExistsError`` when the address is
    already a user.
    """
    if user.role not in config.roles:
        raise ValueError(f"Role '{user.role}' not found")
    assert_is_new_user(config, email)
    config.users[email] = user


def assert_keeps_an_admin(
    config: SettingsConfig,
    email: str,
    *,
    new_role: str | None = None,
    eligible: Collection[str] | None = None,
) -> None:
    """Raise ``LastAdminError`` if this write would zero the admins.

    Called by the config stores INSIDE their write lock, so the check
    and the write are one atomic step. A route-level pre-check cannot
    be: two parallel calls each read "two admins", each pass, and the
    org ends with none. That race is reachable in cloud mode, where an
    assistant issuing two tool calls in one turn is an ordinary event.

    ``eligible`` is the same set the pre-check counts (the accepted
    members): without it, two parallel removals of the two real admins
    both pass and leave only a pending admin invitation.

    The routes still pre-check, purely to return a friendlier status
    and message than a store exception carries. They pass the
    accepted members they counted as ``eligible``, so the store
    refuses what the route would.

    Two counts must each keep an admin: the accepted admins
    (``eligible``) and, when ``eligible`` is given, every listed admin.
    The second only matters when no admin has accepted yet: the
    accepted count is already zero, so it cannot object, and the last
    invited admin is all the org has left.
    """
    if would_remove_last_admin(
        config, email, new_role=new_role, eligible=eligible,
    ) or (
        eligible is not None
        and would_remove_last_admin(config, email, new_role=new_role)
    ):
        raise LastAdminError(
            LAST_ADMIN_DEMOTE_ERROR if new_role is not None
            else LAST_ADMIN_REMOVE_ERROR
        )


def remove_user_from_config(
    config: SettingsConfig,
    email: str,
    *,
    eligible: Collection[str] | None = None,
) -> None:
    """Remove ``email`` from ``config.users``, under every spelling the
    org holds it (``every_spelling``): an org saved before letter case
    was ignored may hold two, and a spelling left behind would read as a
    pending invitation the removed person could accept again.

    Shared by the config stores, which call it INSIDE their write lock
    (see ``assert_keeps_an_admin``). Raises ``ValueError`` when the org
    has no such user, ``LastAdminError`` when it is the last admin.
    """
    spellings = every_spelling(config.users, email)
    if not spellings:
        raise ValueError(f"User '{email}' not found")
    assert_keeps_an_admin(config, email, eligible=eligible)
    for spelling in spellings:
        del config.users[spelling]


def set_user_role_in_config(
    config: SettingsConfig,
    email: str,
    role: str,
    *,
    eligible: Collection[str] | None = None,
) -> None:
    """Give ``email`` the role ``role`` in ``config.users``, under every
    spelling the org holds it, so no spelling keeps the old role.

    Shared by the config stores, like ``remove_user_from_config``.
    Raises ``ValueError`` when the org has no such user or no such role,
    ``LastAdminError`` when it demotes the last admin.
    """
    spellings = every_spelling(config.users, email)
    if not spellings:
        raise ValueError(f"User '{email}' not found")
    if role not in config.roles:
        raise ValueError(f"Role '{role}' not found")
    assert_keeps_an_admin(config, email, new_role=role, eligible=eligible)
    for spelling in spellings:
        config.users[spelling].role = role
