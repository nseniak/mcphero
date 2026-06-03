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

    role_def = config.roles.get(user_def.role)
    if role_def is None:
        return _EMPTY

    s = role_def.settings
    return ResolvedSettings(
        mcp_access=s.mcp_access,
        tool_access=s.tool_access,
        default_arguments=s.default_arguments,
        argument_constraints=s.argument_constraints,
        role_name=user_def.role,
        is_admin=role_def.is_admin,
    )
