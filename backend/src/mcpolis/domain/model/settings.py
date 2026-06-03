from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ArgumentConstraint(BaseModel):
    pattern: str
    mode: Literal["allow", "forbid"] = "allow"


class McpAccessConfig(BaseModel):
    """MCP access configuration: auto-enable flag for new MCPs + per-MCP access map."""

    auto_enable_new: bool = False
    mcps: dict[str, bool] = Field(default_factory=dict)


class ToolAccessConfig(BaseModel):
    """Per-upstream tool access configuration.

    Resolution order:
    1. Category defaults in ``category_defaults`` (deny wins)
    2. Explicit per-tool setting in ``tools``
    3. Catch-all ``fallback_enabled`` (True if None = allow unknown tools)
    """

    fallback_enabled: bool | None = None
    category_defaults: dict[str, bool] = Field(default_factory=dict)
    tools: dict[str, bool] = Field(default_factory=dict)


class RoleSettings(BaseModel):
    """Settings attached to a role. All fields have defaults (no inheritance)."""

    mcp_access: McpAccessConfig = Field(default_factory=McpAccessConfig)
    tool_access: dict[str, ToolAccessConfig] = Field(default_factory=dict)
    default_arguments: dict[str, dict[str, dict[str, object]]] = Field(
        default_factory=dict
    )
    argument_constraints: dict[str, dict[str, ArgumentConstraint]] = Field(
        default_factory=dict
    )


class RoleDefinition(BaseModel):
    is_admin: bool = False
    is_default: bool = False
    settings: RoleSettings = Field(default_factory=RoleSettings)


class UserDefinition(BaseModel):
    role: str


class UpstreamOptions(BaseModel):
    """MCPolis-specific options for an upstream (display name, auth, etc.)."""

    display_name: str = ""
    auth_mode: str = "service_account"
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = Field(default_factory=list)
    default_arguments: dict[str, dict[str, object]] = Field(default_factory=dict)


class OAuthAppEntry(BaseModel):
    """Pre-registered OAuth app credentials for a domain."""
    client_id: str
    client_secret: str


OAuthAppsConfig = dict[str, OAuthAppEntry]


class SettingsConfig(BaseModel):
    upstreams: dict[str, UpstreamOptions] = Field(default_factory=dict)
    roles: dict[str, RoleDefinition] = Field(default_factory=dict)
    users: dict[str, UserDefinition] = Field(default_factory=dict)


DEFAULT_SETTINGS_CONFIG = SettingsConfig(
    roles={
        "admin": RoleDefinition(
            is_admin=True,
            settings=RoleSettings(
                mcp_access=McpAccessConfig(auto_enable_new=True),
            ),
        ),
        "user": RoleDefinition(
            is_default=True,
            settings=RoleSettings(
                mcp_access=McpAccessConfig(auto_enable_new=True),
            ),
        ),
    },
    users={},
)
