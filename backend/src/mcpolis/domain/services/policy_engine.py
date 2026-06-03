from __future__ import annotations

import re
from dataclasses import dataclass

from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    SettingsConfig,
    ToolAccessConfig,
)
from mcpolis.domain.services.settings_resolver import ResolvedSettings, resolve_settings


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    matched_role: str | None = None
    matched_rule: str | None = None


def _resolve_tool_access(
    config: ToolAccessConfig,
    tool_name: str,
    tool_annotations: dict[str, bool],
) -> bool:
    """Resolve whether a tool is allowed given a ToolAccessConfig.

    Resolution order:
    1. Category defaults (deny wins if multiple match)
    2. Explicit per-tool override
    3. Catch-all fallback_enabled (True if None)

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
    def __init__(self, config: SettingsConfig) -> None:
        self._config = config

    def reload(self, config: SettingsConfig) -> None:
        """Hot-reload with a new config."""
        self._config = config

    @property
    def config(self) -> SettingsConfig:
        return self._config

    @property
    def is_empty(self) -> bool:
        """True when no roles are configured (permissive mode)."""
        return len(self._config.roles) == 0

    def get_user_roles(self, user_id: str) -> list[str]:
        """Return role names for the given user."""
        user = self._config.users.get(user_id)
        if user is None:
            return []
        return [user.role]

    def has_role(self, user_id: str, role_name: str) -> bool:
        """Check if a user is assigned to a role with the given name.

        Strict role-name equality. Use :meth:`is_admin` for the
        "is this user an admin?" check — admin-ness is determined
        by ``RoleDefinition.is_admin``, not by the role's name.
        """
        user = self._config.users.get(user_id)
        if user is None:
            return False
        return user.role == role_name

    def is_admin(self, user_id: str) -> bool:
        """Whether the user is admin in this org.

        Resolves the user's role and returns its ``is_admin`` flag.
        Any role flagged ``is_admin=True`` grants admin, regardless
        of the role's name. Unknown users → False.
        """
        return resolve_settings(self._config, user_id).is_admin

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
        """Return sorted list of emails of users who have admin privileges."""
        return sorted(
            email
            for email in self._config.users
            if self.is_admin(email)
        )

    def get_default_role(self) -> str | None:
        """Return the name of the role tagged is_default, or None."""
        for name, role_def in self._config.roles.items():
            if role_def.is_default:
                return name
        return None

    def get_allowed_upstreams(self, user_id: str) -> set[str] | None:
        """Return set of upstream IDs the user can access, or None if all allowed."""
        if self.is_empty:
            return None

        resolved = resolve_settings(self._config, user_id)
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
    ) -> list[tuple[str, str, dict[str, bool]]]:
        """Filter (upstream_id, tool_name, annotation_flags) triples by policy."""
        if self.is_empty:
            return tools

        resolved = resolve_settings(self._config, user_id)
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
    ) -> PolicyDecision:
        """Evaluate whether a user can call a specific tool with given arguments."""
        if self.is_empty:
            return PolicyDecision(allowed=True, reason="no_policy_configured")

        resolved = resolve_settings(self._config, user_id)
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
                )
        else:
            if not matches:
                return PolicyDecision(
                    allowed=False,
                    reason=f"argument '{arg_name}' does not match required pattern",
                    matched_role=role_name,
                )
        return PolicyDecision(
            allowed=True,
            reason="argument_valid",
            matched_role=role_name,
        )
