from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    DEFAULT_SETTINGS_CONFIG,
    McpAccessConfig,
    RoleDefinition,
    SettingsConfig,
    ToolAccessConfig,
    UpstreamOptions,
    UserDefinition,
)


class FileConfigStore:
    """Unified file-based config store (roles, users, upstream options) using JSON."""

    def __init__(self, config_path: Path) -> None:
        self._path = config_path
        self._lock = asyncio.Lock()

    def _read(self) -> SettingsConfig:
        if not self._path.exists():
            return SettingsConfig()
        try:
            raw: Any = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return SettingsConfig()
        if not raw:
            return SettingsConfig()
        return SettingsConfig.model_validate(raw)

    def _write(self, config: SettingsConfig) -> None:
        data = config.model_dump(mode="json", exclude_none=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)

    async def load(self, org_id: str) -> SettingsConfig:
        async with self._lock:
            return self._read()

    async def save(self, org_id: str, config: SettingsConfig) -> None:
        async with self._lock:
            self._write(config)

    async def delete_for_org(self, org_id: str) -> None:
        # Single-org file store (standalone): the config file holds only
        # this org, so removing it IS the per-org purge. Idempotent.
        async with self._lock:
            self._path.unlink(missing_ok=True)

    def ensure_defaults_sync(self, org_id: str) -> SettingsConfig:
        """Synchronous version for use during app startup (no event loop)."""
        config = self._read()
        if not config.roles:
            config = DEFAULT_SETTINGS_CONFIG
            self._write(config)
        return config

    async def ensure_defaults(self, org_id: str) -> SettingsConfig:
        """Create default config if file is missing or empty. Returns the config."""
        async with self._lock:
            config = self._read()
            if not config.roles:
                config = DEFAULT_SETTINGS_CONFIG
                self._write(config)
            return config

    # --- Upstream options ---

    def read_upstream_options_sync(self, org_id: str) -> dict[str, UpstreamOptions]:
        """Synchronous read of upstream options for startup."""
        return self._read().upstreams

    async def set_upstream_options(
        self, org_id: str, upstream_id: str, options: UpstreamOptions
    ) -> SettingsConfig:
        async with self._lock:
            config = self._read()
            config.upstreams[upstream_id] = options
            self._write(config)
            return config

    async def remove_upstream_options(self, org_id: str, upstream_id: str) -> SettingsConfig:
        async with self._lock:
            config = self._read()
            config.upstreams.pop(upstream_id, None)
            self._write(config)
            return config

    # --- Users ---

    async def set_user(self, org_id: str, email: str, user: UserDefinition) -> SettingsConfig:
        async with self._lock:
            config = self._read()
            config.users[email] = user
            self._write(config)
            return config

    async def remove_user(self, org_id: str, email: str) -> SettingsConfig:
        async with self._lock:
            config = self._read()
            if email not in config.users:
                raise ValueError(f"User '{email}' not found")
            del config.users[email]
            self._write(config)
            return config

    async def set_user_role(self, org_id: str, email: str, role: str) -> SettingsConfig:
        async with self._lock:
            config = self._read()
            if email not in config.users:
                raise ValueError(f"User '{email}' not found")
            if role not in config.roles:
                raise ValueError(f"Role '{role}' not found")
            config.users[email].role = role
            self._write(config)
            return config

    # --- Roles ---

    async def set_role_mcp_access(
        self, org_id: str, role_name: str, mcp_access: McpAccessConfig
    ) -> SettingsConfig:
        """Set the entire mcp_access for a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access = mcp_access
            self._write(config)
            return config

    async def set_role_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str, enabled: bool
    ) -> SettingsConfig:
        """Set a single MCP access override on a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.mcps[mcp_id] = enabled
            self._write(config)
            return config

    async def remove_role_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str
    ) -> SettingsConfig:
        """Remove a single MCP access override from a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.mcps.pop(mcp_id, None)
            self._write(config)
            return config

    async def create_role(
        self, org_id: str, name: str, copy_from: str | None = None
    ) -> SettingsConfig:
        """Create a new role, optionally copying settings from an existing role."""
        async with self._lock:
            config = self._read()
            if name in config.roles:
                raise ValueError(f"Role '{name}' already exists")
            if copy_from is not None:
                if copy_from not in config.roles:
                    raise ValueError(f"Source role '{copy_from}' not found")
                source = config.roles[copy_from]
                config.roles[name] = RoleDefinition(
                    settings=source.settings.model_copy(deep=True),
                )
            else:
                config.roles[name] = RoleDefinition()
            self._write(config)
            return config

    async def delete_role(self, org_id: str, name: str) -> SettingsConfig:
        """Delete a role. Fails if any users reference it or if it is
        the org's last role."""
        async with self._lock:
            config = self._read()
            if name not in config.roles:
                raise ValueError(f"Role '{name}' not found")
            users_with_role = [
                email for email, u in config.users.items() if u.role == name
            ]
            if users_with_role:
                raise ValueError(
                    f"Cannot delete role '{name}': {len(users_with_role)} user(s) assigned"
                )
            if len(config.roles) == 1:
                # A zero-roles org denies every identity (PolicyEngine
                # fails closed), so reaching that state is never what
                # an admin wants — refuse rather than brick the org.
                raise ValueError(
                    f"Cannot delete role '{name}': an org must keep at least one role"
                )
            del config.roles[name]
            self._write(config)
            return config

    async def rename_role(
        self, org_id: str, old_name: str, new_name: str
    ) -> SettingsConfig:
        """Rename a role, updating all user references."""
        async with self._lock:
            config = self._read()
            if old_name not in config.roles:
                raise ValueError(f"Role '{old_name}' not found")
            if new_name in config.roles:
                raise ValueError(f"Role '{new_name}' already exists")
            role_def = config.roles.pop(old_name)
            config.roles[new_name] = role_def
            for user_def in config.users.values():
                if user_def.role == old_name:
                    user_def.role = new_name
            self._write(config)
            return config

    async def create_mcp_access(self, org_id: str, mcp_id: str) -> SettingsConfig:
        """Create per-role access entries for a newly added MCP.

        Each role gets an entry based on its auto_enable_new setting,
        and a ToolAccessConfig with all tools enabled by default.
        """
        async with self._lock:
            config = self._read()
            for role in config.roles.values():
                enabled = role.settings.mcp_access.auto_enable_new
                role.settings.mcp_access.mcps[mcp_id] = enabled
                if mcp_id not in role.settings.tool_access:
                    role.settings.tool_access[mcp_id] = ToolAccessConfig(
                        fallback_enabled=True,
                        category_defaults={
                            "readOnly": True,
                            "destructive": True,
                        },
                    )
            self._write(config)
            return config

    async def set_role_auto_enable_new(
        self, org_id: str, role_name: str, auto_enable_new: bool
    ) -> SettingsConfig:
        """Set the auto_enable_new flag for a role's mcp_access."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.auto_enable_new = auto_enable_new
            self._write(config)
            return config

    # --- Tool access ---

    def _get_or_create_tool_access(
        self, role_name: str, upstream_id: str, config: SettingsConfig
    ) -> ToolAccessConfig:
        """Get or create ToolAccessConfig for a role/upstream pair."""
        role = config.roles[role_name]
        if upstream_id not in role.settings.tool_access:
            role.settings.tool_access[upstream_id] = ToolAccessConfig()
        return role.settings.tool_access[upstream_id]

    async def set_role_tool_access_entry(
        self, org_id: str, role_name: str, upstream_id: str, tool_name: str, enabled: bool
    ) -> SettingsConfig:
        """Set a single tool access override on a role for an upstream."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.tools[tool_name] = enabled
            self._write(config)
            return config

    async def remove_role_tool_access_entry(
        self, org_id: str, role_name: str, upstream_id: str, tool_name: str
    ) -> SettingsConfig:
        """Remove a single tool access override from a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            role = config.roles[role_name]
            tac = role.settings.tool_access.get(upstream_id)
            if tac is not None:
                tac.tools.pop(tool_name, None)
            self._write(config)
            return config

    async def set_role_tool_fallback_enabled(
        self, org_id: str, role_name: str, upstream_id: str, fallback_enabled: bool | None
    ) -> SettingsConfig:
        """Set the catch-all fallback_enabled for tools on an upstream for a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.fallback_enabled = fallback_enabled
            self._write(config)
            return config

    async def set_role_tool_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str, enabled: bool
    ) -> SettingsConfig:
        """Set an annotation-based default for tools on an upstream for a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.category_defaults[annotation] = enabled
            self._write(config)
            return config

    async def remove_role_tool_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str
    ) -> SettingsConfig:
        """Remove an annotation-based default for tools on an upstream for a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            role = config.roles[role_name]
            tac = role.settings.tool_access.get(upstream_id)
            if tac is not None:
                tac.category_defaults.pop(annotation, None)
            self._write(config)
            return config

    # --- Argument constraints ---

    async def set_role_argument_constraint(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
        constraint: ArgumentConstraint,
    ) -> SettingsConfig:
        """Set an argument constraint for a tool on a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            key = f"{upstream_id}__{tool_name}"
            constraints = config.roles[role_name].settings.argument_constraints
            if key not in constraints:
                constraints[key] = {}
            constraints[key][arg_name] = constraint
            self._write(config)
            return config

    async def remove_role_argument_constraint(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
    ) -> SettingsConfig:
        """Remove an argument constraint for a tool on a role."""
        async with self._lock:
            config = self._read()
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            key = f"{upstream_id}__{tool_name}"
            constraints = config.roles[role_name].settings.argument_constraints
            if key in constraints:
                constraints[key].pop(arg_name, None)
                if not constraints[key]:
                    del constraints[key]
            self._write(config)
            return config
