"""Mongo-backed ``ConfigRepository`` for cloud mode.

One document per org holds the full ``SettingsConfig`` (roles, users,
upstream options). Reads/writes go through ``OrgScopedCollection`` so
the org filter is enforced centrally.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
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
from mcpolis.domain.ports.config_repository import ConfigRepository


class MongoConfigRepository(ConfigRepository):
    def __init__(self, collection: OrgScopedCollection) -> None:
        self._coll = collection
        # One async lock per process covers the read-modify-write
        # pattern used by every mutation below. Cross-process coordination
        # for multi-backend deploys is Phase 4 (distributed lock).
        self._lock = asyncio.Lock()

    # --- Internal helpers ---

    async def _read(self, org_id: str) -> SettingsConfig:
        doc = await self._coll.find_one(org_id)
        if doc is None or "config" not in doc:
            return SettingsConfig()
        return SettingsConfig.model_validate(doc["config"])

    async def _write(self, org_id: str, config: SettingsConfig) -> None:
        await self._coll.replace_one(
            org_id, {"org_id": org_id},
            {"org_id": org_id, "config": config.model_dump(mode="json")},
            upsert=True,
        )

    # --- Load / save / defaults ---

    async def load(self, org_id: str) -> SettingsConfig:
        async with self._lock:
            return await self._read(org_id)

    async def save(self, org_id: str, config: SettingsConfig) -> None:
        async with self._lock:
            await self._write(org_id, config)

    def ensure_defaults_sync(self, org_id: str) -> SettingsConfig:
        """Sync variant: used at startup before the event loop runs.

        Cloud mode's startup is async-first, so the only caller to this
        method in standalone code paths is shimmed by ``create_app``
        before cloud mode is fully up. We return the baked-in default
        — the first async ``ensure_defaults`` call will persist it.
        """
        return DEFAULT_SETTINGS_CONFIG.model_copy(deep=True)

    async def ensure_defaults(self, org_id: str) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if not config.roles:
                # Seed the default roles but preserve any existing
                # users and upstreams — this method can run against a
                # partially-populated doc (e.g. a half-configured org
                # from an earlier run) and we don't want to silently
                # drop data that the admin has already entered.
                defaults = DEFAULT_SETTINGS_CONFIG.model_copy(deep=True)
                defaults.users = dict(config.users)
                defaults.upstreams = dict(config.upstreams)
                config = defaults
                await self._write(org_id, config)
            return config

    # --- Upstream options ---

    def read_upstream_options_sync(self, org_id: str) -> dict[str, UpstreamOptions]:
        # Mongo variant has no sync path — callers that need this
        # during startup must be reworked. For Phase 2c's scope, no
        # cloud-mode caller hits this method.
        raise RuntimeError(
            "MongoConfigRepository.read_upstream_options_sync is not "
            "supported — use the async variant."
        )

    async def set_upstream_options(
        self, org_id: str, upstream_id: str, options: UpstreamOptions
    ) -> SettingsConfig:
        # Dead in cloud mode: the live admin path persists upstream
        # options through ``MongoUpstreamConfigRepository`` (one
        # document per upstream in the ``upstreams`` collection), not
        # through the per-org ``config`` doc. Standalone still uses the
        # equivalent method on ``FileConfigStore`` because its merged
        # view is built from ``mcp.json`` + ``config.json``. If anything
        # in cloud mode lands here, the call site is wired to the
        # wrong repository — fail loudly rather than silently writing
        # plaintext credentials into the ``config`` collection.
        raise NotImplementedError(
            "MongoConfigRepository.set_upstream_options is not used in "
            "cloud mode; persist upstream options via "
            "MongoUpstreamConfigRepository instead."
        )

    async def remove_upstream_options(
        self, org_id: str, upstream_id: str
    ) -> SettingsConfig:
        raise NotImplementedError(
            "MongoConfigRepository.remove_upstream_options is not used "
            "in cloud mode; remove upstreams via "
            "MongoUpstreamConfigRepository instead."
        )

    # --- Users ---

    async def set_user(
        self, org_id: str, email: str, user: UserDefinition
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            config.users[email] = user
            await self._write(org_id, config)
            return config

    async def remove_user(self, org_id: str, email: str) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if email not in config.users:
                raise ValueError(f"User '{email}' not found")
            del config.users[email]
            await self._write(org_id, config)
            return config

    async def set_user_role(
        self, org_id: str, email: str, role: str
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if email not in config.users:
                raise ValueError(f"User '{email}' not found")
            if role not in config.roles:
                raise ValueError(f"Role '{role}' not found")
            config.users[email].role = role
            await self._write(org_id, config)
            return config

    # --- Roles ---

    async def set_role_mcp_access(
        self, org_id: str, role_name: str, mcp_access: McpAccessConfig
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access = mcp_access
            await self._write(org_id, config)
            return config

    async def set_role_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str, enabled: bool
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.mcps[mcp_id] = enabled
            await self._write(org_id, config)
            return config

    async def remove_role_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.mcps.pop(mcp_id, None)
            await self._write(org_id, config)
            return config

    async def create_role(
        self, org_id: str, name: str, copy_from: str | None = None
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
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
            await self._write(org_id, config)
            return config

    async def delete_role(self, org_id: str, name: str) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
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
            await self._write(org_id, config)
            return config

    async def rename_role(
        self, org_id: str, old_name: str, new_name: str
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if old_name not in config.roles:
                raise ValueError(f"Role '{old_name}' not found")
            if new_name in config.roles:
                raise ValueError(f"Role '{new_name}' already exists")
            role_def = config.roles.pop(old_name)
            config.roles[new_name] = role_def
            for user_def in config.users.values():
                if user_def.role == old_name:
                    user_def.role = new_name
            await self._write(org_id, config)
            return config

    async def create_mcp_access(
        self, org_id: str, mcp_id: str
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
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
            await self._write(org_id, config)
            return config

    async def set_role_auto_enable_new(
        self, org_id: str, role_name: str, auto_enable_new: bool
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.auto_enable_new = auto_enable_new
            await self._write(org_id, config)
            return config

    # --- Tool access ---

    def _get_or_create_tool_access(
        self, role_name: str, upstream_id: str, config: SettingsConfig
    ) -> ToolAccessConfig:
        role = config.roles[role_name]
        if upstream_id not in role.settings.tool_access:
            role.settings.tool_access[upstream_id] = ToolAccessConfig()
        return role.settings.tool_access[upstream_id]

    async def set_role_tool_access_entry(
        self, org_id: str, role_name: str, upstream_id: str, tool_name: str, enabled: bool
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.tools[tool_name] = enabled
            await self._write(org_id, config)
            return config

    async def remove_role_tool_access_entry(
        self, org_id: str, role_name: str, upstream_id: str, tool_name: str
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            role = config.roles[role_name]
            tac = role.settings.tool_access.get(upstream_id)
            if tac is not None:
                tac.tools.pop(tool_name, None)
            await self._write(org_id, config)
            return config

    async def set_role_tool_fallback_enabled(
        self, org_id: str, role_name: str, upstream_id: str, fallback_enabled: bool | None
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.fallback_enabled = fallback_enabled
            await self._write(org_id, config)
            return config

    async def set_role_tool_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str, enabled: bool
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.category_defaults[annotation] = enabled
            await self._write(org_id, config)
            return config

    async def remove_role_tool_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            role = config.roles[role_name]
            tac = role.settings.tool_access.get(upstream_id)
            if tac is not None:
                tac.category_defaults.pop(annotation, None)
            await self._write(org_id, config)
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
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            key = f"{upstream_id}__{tool_name}"
            constraints = config.roles[role_name].settings.argument_constraints
            if key not in constraints:
                constraints[key] = {}
            constraints[key][arg_name] = constraint
            await self._write(org_id, config)
            return config

    async def remove_role_argument_constraint(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
    ) -> SettingsConfig:
        async with self._lock:
            config = await self._read(org_id)
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            key = f"{upstream_id}__{tool_name}"
            constraints = config.roles[role_name].settings.argument_constraints
            if key in constraints:
                constraints[key].pop(arg_name, None)
                if not constraints[key]:
                    del constraints[key]
            await self._write(org_id, config)
            return config


# Silence unused-import complaints — these are part of the public
# surface used by callers constructing default configs.
_: Any = (DEFAULT_SETTINGS_CONFIG, RoleDefinition, UserDefinition, ArgumentConstraint)
