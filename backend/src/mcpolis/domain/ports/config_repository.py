from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    OrgUserEntry,
    SettingsConfig,
    UpstreamOptions,
    UserDefinition,
)


CONFIG_WRITE_CONFLICT_MESSAGE = (
    "The organization's settings changed while saving. "
    "Nothing was saved. Try again."
)


class ConfigWriteConflictError(RuntimeError):
    """The org's settings kept changing under a write, so nothing was
    written. Retrying the same request is safe."""


class UserAlreadyExistsError(ValueError):
    """``set_user`` was given an address that is already a user."""


class ConfigRepository(Protocol):
    async def load(self, org_id: str) -> SettingsConfig: ...

    async def save(self, org_id: str, config: SettingsConfig) -> None: ...

    async def delete_for_org(self, org_id: str) -> None:
        """Drop this org's entire config document (users, roles,
        upstream options). Part of the org-deletion cascade. Idempotent.
        """
        ...

    def ensure_defaults_sync(self, org_id: str) -> SettingsConfig: ...

    async def ensure_defaults(self, org_id: str) -> SettingsConfig: ...

    def read_upstream_options_sync(self, org_id: str) -> dict[str, UpstreamOptions]: ...

    async def set_upstream_options(
        self, org_id: str, upstream_id: str, options: UpstreamOptions
    ) -> SettingsConfig: ...

    async def remove_upstream_options(
        self, org_id: str, upstream_id: str
    ) -> SettingsConfig: ...

    async def remove_upstream_role_rules(
        self, org_id: str, upstream_id: str
    ) -> SettingsConfig:
        """Drop the upstream's access entry, tool access overrides and
        argument checks from every role. Part of removing an upstream.
        Idempotent."""
        ...

    async def find_user(self, email: str) -> list[OrgUserEntry]:
        """Every org whose users include ``email``, letter case ignored:
        its members and its invitations not accepted yet.

        The dashboard asks on every page load (``/api/auth/me``), so an
        answer must not read every org's config."""
        ...

    # The user methods below find ``email`` ignoring letter case, and act
    # on the entry under the spelling the org stores.

    async def set_user(
        self, org_id: str, email: str, user: UserDefinition
    ) -> SettingsConfig:
        """Add a new user. Raises ``UserAlreadyExistsError`` if the
        address is already a user (letter case ignored): changing a role
        goes through ``set_user_role``, which keeps the last-admin rule.
        Raises ``ValueError`` when ``user.role`` is not a role of the
        org. Both are checked in the store's atomic write step, because
        a caller's earlier check can be stale."""
        ...

    async def add_first_user(
        self, org_id: str, email: str, user: UserDefinition,
    ) -> SettingsConfig | None:
        """Add ``email`` only if the org has no users yet, as one atomic
        step; None (nothing written) when it already has some. Used by a
        fresh standalone install's first sign-in, which becomes its
        admin: two first sign-ins at once must not both become admin."""
        ...

    async def remove_user(
        self,
        org_id: str,
        email: str,
        *,
        eligible: Collection[str] | None = None,
    ) -> SettingsConfig:
        """Remove a user, under every spelling of their address the org's
        users hold (``remove_user_from_config``). Raises ``LastAdminError``
        when it would leave no admin among ``eligible`` (every user when
        None): checked under the store's lock, so two parallel removals
        can't both pass."""
        ...

    async def set_user_role(
        self,
        org_id: str,
        email: str,
        role: str,
        *,
        eligible: Collection[str] | None = None,
    ) -> SettingsConfig:
        """Change a user's role, under every spelling of their address,
        with the same last-admin check as ``remove_user``."""
        ...

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
    ) -> SettingsConfig:
        """Give every role fresh access entries for a newly added MCP,
        after dropping any rules still stored under that id."""
        ...

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
