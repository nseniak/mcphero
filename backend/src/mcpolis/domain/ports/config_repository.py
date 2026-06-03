from __future__ import annotations

from typing import Protocol

from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    SettingsConfig,
    UpstreamOptions,
    UserDefinition,
)


class ConfigRepository(Protocol):
    async def load(self, org_id: str) -> SettingsConfig: ...

    async def save(self, org_id: str, config: SettingsConfig) -> None: ...

    def ensure_defaults_sync(self, org_id: str) -> SettingsConfig: ...

    async def ensure_defaults(self, org_id: str) -> SettingsConfig: ...

    def read_upstream_options_sync(self, org_id: str) -> dict[str, UpstreamOptions]: ...

    async def set_upstream_options(
        self, org_id: str, upstream_id: str, options: UpstreamOptions
    ) -> SettingsConfig: ...

    async def remove_upstream_options(
        self, org_id: str, upstream_id: str
    ) -> SettingsConfig: ...

    async def set_user(
        self, org_id: str, email: str, user: UserDefinition
    ) -> SettingsConfig: ...

    async def remove_user(self, org_id: str, email: str) -> SettingsConfig: ...

    async def set_user_role(
        self, org_id: str, email: str, role: str
    ) -> SettingsConfig: ...

    async def set_role_mcp_access(
        self, org_id: str, role_name: str, mcp_access: McpAccessConfig
    ) -> SettingsConfig: ...

    async def set_role_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str, enabled: bool
    ) -> SettingsConfig: ...

    async def remove_role_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str
    ) -> SettingsConfig: ...

    async def create_role(
        self, org_id: str, name: str, copy_from: str | None = None
    ) -> SettingsConfig: ...

    async def delete_role(self, org_id: str, name: str) -> SettingsConfig: ...

    async def rename_role(
        self, org_id: str, old_name: str, new_name: str
    ) -> SettingsConfig: ...

    async def create_mcp_access(
        self, org_id: str, mcp_id: str
    ) -> SettingsConfig: ...

    async def set_role_auto_enable_new(
        self, org_id: str, role_name: str, auto_enable_new: bool
    ) -> SettingsConfig: ...

    async def set_role_tool_access_entry(
        self, org_id: str, role_name: str, upstream_id: str, tool_name: str, enabled: bool
    ) -> SettingsConfig: ...

    async def remove_role_tool_access_entry(
        self, org_id: str, role_name: str, upstream_id: str, tool_name: str
    ) -> SettingsConfig: ...

    async def set_role_tool_fallback_enabled(
        self, org_id: str, role_name: str, upstream_id: str, fallback_enabled: bool | None
    ) -> SettingsConfig: ...

    async def set_role_tool_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str, enabled: bool
    ) -> SettingsConfig: ...

    async def remove_role_tool_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str
    ) -> SettingsConfig: ...

    async def set_role_argument_constraint(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
        constraint: ArgumentConstraint,
    ) -> SettingsConfig: ...

    async def remove_role_argument_constraint(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
    ) -> SettingsConfig: ...
