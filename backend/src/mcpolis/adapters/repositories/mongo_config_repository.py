"""Mongo-backed ``ConfigRepository`` for cloud mode.

One document per org holds the full ``SettingsConfig`` (roles, users,
upstream options). Reads/writes go through ``OrgScopedCollection`` so
the org filter is enforced centrally.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Collection
from typing import Any

import structlog
from pydantic import TypeAdapter, ValidationError
from pymongo.errors import DuplicateKeyError

from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.adapters.repositories.user_index import UserIndex
from mcpolis.domain.services.upstream_role_rules import remove_upstream_role_rules
from mcpolis.domain.services.settings_resolver import (
    add_user_to_config,
    remove_user_from_config,
    set_user_role_in_config,
)
from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    DEFAULT_SETTINGS_CONFIG,
    McpAccessConfig,
    OrgUserEntry,
    RoleDefinition,
    SettingsConfig,
    ToolAccessConfig,
    UpstreamOptions,
    UserDefinition,
)
from mcpolis.domain.ports.config_repository import (
    CONFIG_WRITE_CONFLICT_MESSAGE,
    ConfigRepository,
    ConfigWriteConflictError,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_USERS: TypeAdapter[dict[str, UserDefinition]] = TypeAdapter(
    dict[str, UserDefinition],
)

# A write that loses a race re-reads and re-applies its change. Each
# retry means another writer committed in between, so running out
# needs that many writers committing back to back on one org.
_MAX_WRITE_ATTEMPTS = 20


class _OrgHasUsers(Exception):
    """``add_first_user`` found the org already has users."""


def _next_rev(rev: object) -> int:
    """The revision after ``rev``. A number edited in by hand (3.0)
    continues from its value; anything else restarts at 1."""
    if isinstance(rev, bool) or not isinstance(rev, int | float):
        return 1
    if isinstance(rev, float) and not math.isfinite(rev):
        return 1
    return int(rev) + 1


def _config_of(doc: dict[str, Any] | None) -> SettingsConfig:
    if doc is None or "config" not in doc:
        return SettingsConfig()
    return SettingsConfig.model_validate(doc["config"])


class MongoConfigRepository(ConfigRepository):
    """Every mutation is read → change → conditional write.

    The org's document carries a ``rev`` counter. A write only lands if
    ``rev`` is still the value it read; otherwise another writer
    committed in between (another request, or another backend process),
    and the change is re-applied to the fresh document. So a check made
    inside a change (for example "this would remove the last admin")
    always holds for the document actually written, across processes.
    The in-process lock only cuts down needless retries.

    Anything else that writes this collection (a migration, a script, a
    repair by hand) must also change ``rev``, or replace the whole
    document. A write that leaves ``rev`` as it was can be undone by a
    backend that read the document before it.
    """

    def __init__(self, collection: OrgScopedCollection) -> None:
        self._coll = collection
        self._lock = asyncio.Lock()
        # Every org's users, for ``find_user``. Kept current by ``load``
        # and ``_mutate``, both under the lock, which every change of an
        # org's users goes through. Per process, like the lock.
        self._users = UserIndex()

    # --- Internal helpers ---

    async def _read(self, org_id: str) -> SettingsConfig:
        return _config_of(await self._coll.find_one(org_id))

    async def _write_if_unchanged(
        self, org_id: str, read_doc: dict[str, Any] | None,
        config: SettingsConfig,
    ) -> bool:
        """Write ``config`` only if the document is still ``read_doc``'s
        revision. Returns False when another writer got there first."""
        body = config.model_dump(mode="json")
        if read_doc is None:
            try:
                await self._coll.insert_one(
                    org_id, {"org_id": org_id, "config": body, "rev": 1},
                )
            except DuplicateKeyError:
                return False
            return True
        if "rev" in read_doc:
            # Exact match on whatever was read, so a value written by
            # hand (a double, a null, a string) still works.
            rev = read_doc["rev"]
            precondition: dict[str, Any] = {"rev": {"$eq": rev}}
            new_rev = _next_rev(rev)
        else:
            precondition = {"rev": {"$exists": False}}
            new_rev = 1
        matched = await self._coll.replace_one(
            org_id, precondition,
            {"org_id": org_id, "config": body, "rev": new_rev},
            upsert=False,
        )
        return matched == 1

    async def _mutate(
        self, org_id: str, change: Callable[[SettingsConfig], SettingsConfig],
    ) -> SettingsConfig:
        """Apply ``change`` to the org's current settings and save the
        result atomically. ``change`` may run more than once (on a
        lost race), so it must only touch the config it is given; it
        may raise to refuse the write."""
        async with self._lock:
            for _ in range(_MAX_WRITE_ATTEMPTS):
                doc = await self._coll.find_one(org_id)
                config = change(_config_of(doc))
                if await self._write_if_unchanged(org_id, doc, config):
                    self._users.set_org(org_id, config.users)
                    return config
        logger.warning(
            "config.write_conflict",
            org_id=org_id, attempts=_MAX_WRITE_ATTEMPTS,
        )
        # The message reaches people (the Admin MCP shows it to the AI
        # client as is), so it names no org id.
        raise ConfigWriteConflictError(CONFIG_WRITE_CONFLICT_MESSAGE)

    async def _index_every_org(self) -> None:
        """Read every org's users into the index: once per process, at
        the first ``find_user``. One query for all the orgs, where
        listing someone's invitations used to load every org's config,
        one at a time behind the lock, on each dashboard page load.

        The cross-org read is the org scoping's named exception, used by
        the operator views; nothing of it leaves the store but the
        entries of the address ``find_user`` is asked about, as when
        every org's config was loaded to find them."""
        for doc in await self._coll.find_many_cross_org({}):
            org_id = doc.get("org_id")
            config = doc.get("config")
            if not isinstance(org_id, str) or not isinstance(config, dict):
                continue
            try:
                users = _USERS.validate_python(
                    config.get("users", {}),  # pyright: ignore[reportUnknownMemberType]
                )
            except ValidationError:
                logger.warning("config.user_index.org_skipped", org_id=org_id)
                continue
            self._users.set_org(org_id, users)
        self._users.complete = True

    # --- Users across orgs ---

    async def find_user(self, email: str) -> list[OrgUserEntry]:
        if not self._users.complete:
            async with self._lock:
                if not self._users.complete:
                    await self._index_every_org()
        return self._users.find(email)

    # --- Load / save / defaults ---

    async def load(self, org_id: str) -> SettingsConfig:
        # Under the lock, like every write: a read that refreshes the
        # user index must not finish after a later write and put back
        # the user list that write replaced.
        async with self._lock:
            config = await self._read(org_id)
            self._users.set_org(org_id, config.users)
            return config

    async def save(self, org_id: str, config: SettingsConfig) -> None:
        await self._mutate(org_id, lambda _current: config)

    async def delete_for_org(self, org_id: str) -> None:
        # Org-deletion cascade. One config doc per org; ``delete_many``
        # with an empty filter is org-scoped by ``OrgScopedCollection``.
        async with self._lock:
            await self._coll.delete_many(org_id, {})
            self._users.drop_org(org_id)

    def ensure_defaults_sync(self, org_id: str) -> SettingsConfig:
        """Sync variant: used at startup before the event loop runs.

        Cloud mode's startup is async-first, so the only caller to this
        method in standalone code paths is shimmed by ``create_app``
        before cloud mode is fully up. We return the baked-in default
        — the first async ``ensure_defaults`` call will persist it.
        """
        return DEFAULT_SETTINGS_CONFIG.model_copy(deep=True)

    async def ensure_defaults(self, org_id: str) -> SettingsConfig:
        config = await self.load(org_id)
        if config.roles:
            return config

        def change(config: SettingsConfig) -> SettingsConfig:
            if config.roles:
                return config  # Seeded by a concurrent writer.
            # Seed the default roles but preserve any existing
            # users and upstreams — this method can run against a
            # partially-populated doc (e.g. a half-configured org
            # from an earlier run) and we don't want to silently
            # drop data that the admin has already entered.
            defaults = DEFAULT_SETTINGS_CONFIG.model_copy(deep=True)
            defaults.users = dict(config.users)
            defaults.upstreams = dict(config.upstreams)
            return defaults

        return await self._mutate(org_id, change)

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

    async def remove_upstream_role_rules(
        self, org_id: str, upstream_id: str
    ) -> SettingsConfig:
        # Unlike the upstream options above, role rules DO live in the
        # per-org ``config`` doc in cloud mode.
        def change(config: SettingsConfig) -> SettingsConfig:
            remove_upstream_role_rules(config, upstream_id)
            return config

        return await self._mutate(org_id, change)

    # --- Users ---

    async def set_user(
        self, org_id: str, email: str, user: UserDefinition
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            # Checked on the document the write replaces (``_mutate``).
            add_user_to_config(config, email, user)
            return config

        return await self._mutate(org_id, change)

    async def add_first_user(
        self, org_id: str, email: str, user: UserDefinition,
    ) -> SettingsConfig | None:
        def change(config: SettingsConfig) -> SettingsConfig:
            if config.users:
                raise _OrgHasUsers
            add_user_to_config(config, email, user)
            return config

        try:
            return await self._mutate(org_id, change)
        except _OrgHasUsers:
            return None

    async def remove_user(
        self,
        org_id: str,
        email: str,
        *,
        eligible: Collection[str] | None = None,
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            # Checked on the exact document the write replaces (see
            # ``_mutate``), so two parallel removals, even from two
            # backend processes, can't each see a surviving admin.
            remove_user_from_config(config, email, eligible=eligible)
            return config

        return await self._mutate(org_id, change)

    async def set_user_role(
        self,
        org_id: str,
        email: str,
        role: str,
        *,
        eligible: Collection[str] | None = None,
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            set_user_role_in_config(config, email, role, eligible=eligible)
            return config

        return await self._mutate(org_id, change)

    # --- Roles ---

    async def set_role_mcp_access(
        self, org_id: str, role_name: str, mcp_access: McpAccessConfig
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access = mcp_access
            return config

        return await self._mutate(org_id, change)

    async def set_role_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str, enabled: bool
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.mcps[mcp_id] = enabled
            return config

        return await self._mutate(org_id, change)

    async def remove_role_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.mcps.pop(mcp_id, None)
            return config

        return await self._mutate(org_id, change)

    async def create_role(
        self, org_id: str, name: str, copy_from: str | None = None
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
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
            return config

        return await self._mutate(org_id, change)

    async def delete_role(self, org_id: str, name: str) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
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
            return config

        return await self._mutate(org_id, change)

    async def rename_role(
        self, org_id: str, old_name: str, new_name: str
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if old_name not in config.roles:
                raise ValueError(f"Role '{old_name}' not found")
            if new_name in config.roles:
                raise ValueError(f"Role '{new_name}' already exists")
            role_def = config.roles.pop(old_name)
            config.roles[new_name] = role_def
            for user_def in config.users.values():
                if user_def.role == old_name:
                    user_def.role = new_name
            return config

        return await self._mutate(org_id, change)

    async def create_mcp_access(
        self, org_id: str, mcp_id: str
    ) -> SettingsConfig:
        # Same reset-then-create as FileConfigStore.create_mcp_access.
        def change(config: SettingsConfig) -> SettingsConfig:
            remove_upstream_role_rules(config, mcp_id)
            for role in config.roles.values():
                enabled = role.settings.mcp_access.auto_enable_new
                role.settings.mcp_access.mcps[mcp_id] = enabled
                role.settings.tool_access[mcp_id] = ToolAccessConfig(
                    fallback_enabled=True,
                    category_defaults={
                        "readOnly": True,
                        "destructive": True,
                    },
                )
            return config

        return await self._mutate(org_id, change)

    async def set_role_auto_enable_new(
        self, org_id: str, role_name: str, auto_enable_new: bool
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            config.roles[role_name].settings.mcp_access.auto_enable_new = auto_enable_new
            return config

        return await self._mutate(org_id, change)

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
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.tools[tool_name] = enabled
            return config

        return await self._mutate(org_id, change)

    async def remove_role_tool_access_entry(
        self, org_id: str, role_name: str, upstream_id: str, tool_name: str
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            role = config.roles[role_name]
            tac = role.settings.tool_access.get(upstream_id)
            if tac is not None:
                tac.tools.pop(tool_name, None)
            return config

        return await self._mutate(org_id, change)

    async def set_role_tool_fallback_enabled(
        self, org_id: str, role_name: str, upstream_id: str, fallback_enabled: bool | None
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.fallback_enabled = fallback_enabled
            return config

        return await self._mutate(org_id, change)

    async def set_role_tool_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str, enabled: bool
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            tac = self._get_or_create_tool_access(role_name, upstream_id, config)
            tac.category_defaults[annotation] = enabled
            return config

        return await self._mutate(org_id, change)

    async def remove_role_tool_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            role = config.roles[role_name]
            tac = role.settings.tool_access.get(upstream_id)
            if tac is not None:
                tac.category_defaults.pop(annotation, None)
            return config

        return await self._mutate(org_id, change)

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
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            key = f"{upstream_id}__{tool_name}"
            constraints = config.roles[role_name].settings.argument_constraints
            if key not in constraints:
                constraints[key] = {}
            constraints[key][arg_name] = constraint
            return config

        return await self._mutate(org_id, change)

    async def remove_role_argument_constraint(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
    ) -> SettingsConfig:
        def change(config: SettingsConfig) -> SettingsConfig:
            if role_name not in config.roles:
                raise ValueError(f"Role '{role_name}' not found")
            key = f"{upstream_id}__{tool_name}"
            constraints = config.roles[role_name].settings.argument_constraints
            if key in constraints:
                constraints[key].pop(arg_name, None)
                if not constraints[key]:
                    del constraints[key]
            return config

        return await self._mutate(org_id, change)


# Silence unused-import complaints — these are part of the public
# surface used by callers constructing default configs.
_: Any = (DEFAULT_SETTINGS_CONFIG, RoleDefinition, UserDefinition, ArgumentConstraint)
