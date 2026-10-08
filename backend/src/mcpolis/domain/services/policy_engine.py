from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from mcpolis.domain.model.email_address import email_key, find_address
from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    SettingsConfig,
    ToolAccessConfig,
)
from mcpolis.domain.services.settings_resolver import (
    NO_ROLE as _NO_ROLE,
    ResolvedSettings,
    resolve_settings,
    resolve_settings_for_role,
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    matched_role: str | None = None
    matched_rule: str | None = None
    # The argument whose value broke an argument pattern, on such a denial.
    matched_argument: str | None = None


def _resolve_tool_access(
    config: ToolAccessConfig,
    tool_name: str,
    tool_annotations: dict[str, bool],
) -> bool:
    """Resolve whether a tool is allowed given a ToolAccessConfig.

    Resolution order:
    1. Category defaults (deny wins if multiple match)
    2. Explicit per-tool override
    3. Catch-all fallback_enabled (None = deny tools not listed)

    Category defaults represent category-level policy and take
    precedence over per-tool overrides (including inherited ones).
    """
    # 1. Category defaults
    if config.category_defaults and tool_annotations:
        matched: list[bool] = []
        for annotation_key, annotation_value in tool_annotations.items():
            if annotation_key in config.category_defaults and annotation_value:
                matched.append(config.category_defaults[annotation_key])
        if matched:
            # Deny wins: if any matched annotation default is False, deny
            return all(matched)

    # 2. Explicit per-tool setting
    if tool_name in config.tools:
        return config.tools[tool_name]

    # 3. Catch-all fallback (None = per-tool mode, deny unknown tools)
    if config.fallback_enabled is not None:
        return config.fallback_enabled
    return False


class PolicyEngine:
    """Who may do what in one org.

    ``config.users`` holds the org's members AND its pending invitations:
    an admin adds an address there when they invite it. An invitation
    becomes a membership only when the invited person accepts it, which
    saves their membership row. ``members`` mirrors those rows: an
    address in ``config.users`` that is not in it is only invited, and
    gets no role here (no tools, no admin rights) until it accepts.

    ``members=None`` means membership is not tracked (engines built
    straight from a config, as many tests do): everyone in
    ``config.users`` is then a member.

    Addresses compare ignoring letter case: an invitation typed as
    ``Bob@Acme.com`` is accepted, and used, by ``bob@acme.com``. A
    member's own spelling (the address they accepted with, which their
    sign-ins are kept under) is what ``get_admin_emails`` and
    ``address_of`` give back.
    """

    def __init__(
        self,
        config: SettingsConfig,
        members: Iterable[str] | None = None,
    ) -> None:
        self._config = config
        # email_key -> the member's own spelling of their address.
        self._members: dict[str, str] | None = (
            {email_key(email): email for email in members}
            if members is not None else None
        )

    def reload(self, config: SettingsConfig) -> None:
        """Hot-reload with a new config. The members are kept."""
        self._config = config

    @property
    def config(self) -> SettingsConfig:
        return self._config

    def set_members(self, members: Iterable[str]) -> None:
        """Replace the accepted members (the org's membership rows)."""
        self._members = {email_key(email): email for email in members}

    def add_member(self, email: str) -> None:
        """``email`` accepted its invitation. No-op while membership is
        not tracked."""
        if self._members is not None:
            self._members[email_key(email)] = email

    def discard_member(self, email: str) -> None:
        """``email`` is no longer a member (removed from the org)."""
        if self._members is not None:
            self._members.pop(email_key(email), None)

    def _user_key(self, user_id: str) -> str | None:
        """``user_id``'s key in ``config.users``, letter case ignored."""
        return find_address(self._config.users, user_id)

    def is_member(self, user_id: str) -> bool:
        """Whether ``user_id`` is in the org's users AND accepted its
        invitation. A pending invitation is not a membership."""
        if self._user_key(user_id) is None:
            return False
        return self._members is None or email_key(user_id) in self._members

    def address_of(self, user_key: str) -> str:
        """The address the person at ``user_key`` (a key of
        ``config.users``) signs in with: their own spelling once they
        accepted, else the key itself. Their sign-ins, sessions and
        tokens are kept under it."""
        if self._members is None:
            return user_key
        return self._members.get(email_key(user_key), user_key)

    @property
    def is_empty(self) -> bool:
        """True when no roles are configured.

        Carries no access semantics: a zero-roles org denies every
        identity (no role resolves → ``_EMPTY`` → no upstreams, no
        tools), exactly like a role-less user in a roled org.
        """
        return len(self._config.roles) == 0

    def get_user_roles(self, user_id: str) -> list[str]:
        """Return role names for the given member ([] for anyone else,
        a pending invitation included)."""
        key = self._user_key(user_id)
        if key is None or not self.is_member(user_id):
            return []
        return [self._config.users[key].role]

    def has_role(self, user_id: str, role_name: str) -> bool:
        """Check if a member is assigned to a role with the given name.

        Strict role-name equality. Use :meth:`is_admin` for the
        "is this user an admin?" check — admin-ness is determined
        by ``RoleDefinition.is_admin``, not by the role's name.
        """
        return role_name in self.get_user_roles(user_id)

    def is_admin(self, user_id: str) -> bool:
        """Whether the user is admin in this org.

        Resolves the member's role and returns its ``is_admin`` flag.
        Any role flagged ``is_admin=True`` grants admin, regardless
        of the role's name. Unknown users and pending invitations →
        False.
        """
        return self._resolve(user_id, None).is_admin

    def admin_role_names(self) -> set[str]:
        """Names of every role flagged ``is_admin=True`` in this config."""
        return {
            name for name, role_def in self._config.roles.items()
            if role_def.is_admin
        }

    def default_admin_role_name(self) -> str:
        """Name of the seed-time admin role for this config.

        Used by org-creation seed code that needs to assign the
        creator to "the admin role" without baking in the literal
        string ``"admin"``. Picks the lexicographically-first admin
        role for determinism when more than one exists. Raises
        ``ValueError`` if the config has no admin role at all (a
        misconfiguration: ``ensure_defaults`` always seeds one).
        """
        names = self.admin_role_names()
        if not names:
            raise ValueError("policy config has no admin role")
        return min(names)

    def get_admin_emails(self) -> list[str]:
        """Return sorted list of emails of members who have admin
        privileges (a pending admin invitation is not one), each as the
        member signs in with it (``address_of``): callers look up the
        admins' upstream sign-ins with them."""
        return sorted(
            self.address_of(email)
            for email in self._config.users
            if self.is_admin(email)
        )

    def get_default_role(self) -> str | None:
        """Return the name of the role tagged is_default, or None."""
        for name, role_def in self._config.roles.items():
            if role_def.is_default:
                return name
        return None

    def _resolve(
        self, user_id: str, boundary_role: str | None
    ) -> ResolvedSettings:
        """Resolve settings for a request identity.

        ``boundary_role`` carries a role established at the auth
        boundary (service tokens); when present it wins over the
        ``config.users`` lookup — the identity (``svc:<label>``) has
        no users entry by design. A pending invitation resolves to
        no role at all.
        """
        if boundary_role is not None:
            return resolve_settings_for_role(self._config, boundary_role)
        if not self.is_member(user_id):
            return _NO_ROLE
        return resolve_settings(self._config, user_id)

    def get_allowed_upstreams(
        self,
        user_id: str,
        *,
        boundary_role: str | None = None,
    ) -> set[str]:
        """Return the set of upstream IDs the identity can access.

        An identity that resolves to no role — unknown user, deleted
        role, or an org with zero roles configured — gets the empty
        set. There is no permissive fallback: org membership on
        ``/mcp/{slug}`` is enforced by policy, so an allow-all path
        here would open a zero-roles org to any authenticated user
        of the platform.
        """
        resolved = self._resolve(user_id, boundary_role)
        if not resolved.role_name:
            return set()

        mcp_access = resolved.mcp_access
        return {
            mcp_id for mcp_id, enabled in mcp_access.mcps.items() if enabled
        }

    def filter_tools(
        self,
        user_id: str,
        tools: list[tuple[str, str, dict[str, bool]]],
        *,
        boundary_role: str | None = None,
    ) -> list[tuple[str, str, dict[str, bool]]]:
        """Filter (upstream_id, tool_name, annotation_flags) triples by policy.

        No-role identities (including every identity in a zero-roles
        org) get an empty list; see ``get_allowed_upstreams``.
        """
        resolved = self._resolve(user_id, boundary_role)
        if not resolved.role_name:
            return []

        result: list[tuple[str, str, dict[str, bool]]] = []
        for upstream_id, tool_name, annotations in tools:
            if not self._has_mcp_access(resolved, upstream_id):
                continue
            if not self._is_tool_allowed(resolved, upstream_id, tool_name, annotations):
                continue
            result.append((upstream_id, tool_name, annotations))
        return result

    def decide_tool_call(
        self,
        user_id: str,
        upstream_id: str,
        tool_name: str,
        arguments: dict[str, object],
        tool_annotations: dict[str, bool] | None = None,
        *,
        boundary_role: str | None = None,
    ) -> PolicyDecision:
        """Evaluate whether a user can call a specific tool with given arguments.

        No-role identities (including every identity in a zero-roles
        org) are denied; see ``get_allowed_upstreams``.
        """
        resolved = self._resolve(user_id, boundary_role)
        if not resolved.role_name:
            return PolicyDecision(allowed=False, reason="user_not_in_any_role")

        if not self._has_mcp_access(resolved, upstream_id):
            return PolicyDecision(
                allowed=False,
                reason=f"MCP '{upstream_id}' not allowed",
                matched_role=resolved.role_name,
            )

        if not self._is_tool_allowed(
            resolved, upstream_id, tool_name, tool_annotations or {}
        ):
            return PolicyDecision(
                allowed=False,
                reason=f"tool '{tool_name}' on MCP '{upstream_id}' not allowed",
                matched_role=resolved.role_name,
            )

        # Check argument constraints
        constraint_decision = self._check_constraints(
            resolved, upstream_id, tool_name, arguments
        )
        if constraint_decision is not None:
            return constraint_decision

        return PolicyDecision(
            allowed=True,
            reason="allowed_by_policy",
            matched_role=resolved.role_name,
        )

    def _has_mcp_access(self, resolved: ResolvedSettings, upstream_id: str) -> bool:
        mcp_access = resolved.mcp_access
        return mcp_access.mcps.get(upstream_id, False)

    def _is_tool_allowed(
        self,
        resolved: ResolvedSettings,
        upstream_id: str,
        tool_name: str,
        tool_annotations: dict[str, bool],
    ) -> bool:
        config = resolved.tool_access.get(upstream_id)
        if config is None:
            return True  # No tool restrictions = all tools allowed
        return _resolve_tool_access(config, tool_name, tool_annotations)

    def _check_constraints(
        self,
        resolved: ResolvedSettings,
        upstream_id: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> PolicyDecision | None:
        # Check constraints keyed by "upstream__tool" pattern
        key = f"{upstream_id}__{tool_name}"
        constraints = resolved.argument_constraints.get(key, {})
        for arg_name, constraint in constraints.items():
            if arg_name not in arguments:
                continue
            arg_value = str(arguments[arg_name])
            decision = self._check_argument(
                arg_name, arg_value, constraint, resolved.role_name
            )
            if not decision.allowed:
                return decision

        # Also check constraints keyed by just the tool name (for wildcard MCP)
        constraints_by_tool = resolved.argument_constraints.get(tool_name, {})
        for arg_name, constraint in constraints_by_tool.items():
            if arg_name not in arguments:
                continue
            arg_value = str(arguments[arg_name])
            decision = self._check_argument(
                arg_name, arg_value, constraint, resolved.role_name
            )
            if not decision.allowed:
                return decision

        return None

    def _check_argument(
        self,
        arg_name: str,
        arg_value: str,
        constraint: ArgumentConstraint,
        role_name: str,
    ) -> PolicyDecision:
        matches = bool(re.search(constraint.pattern, arg_value))
        if constraint.mode == "forbid":
            if re.search(constraint.pattern, arg_value, re.IGNORECASE):
                return PolicyDecision(
                    allowed=False,
                    reason=f"argument '{arg_name}' matches forbidden pattern",
                    matched_role=role_name,
                    matched_argument=arg_name,
                )
        else:
            if not matches:
                return PolicyDecision(
                    allowed=False,
                    reason=f"argument '{arg_name}' does not match required pattern",
                    matched_role=role_name,
                    matched_argument=arg_name,
                )
        return PolicyDecision(
            allowed=True,
            reason="argument_valid",
            matched_role=role_name,
        )
