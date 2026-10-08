"""Role actions, shared by the dashboard's Roles and Access pages and the
Admin MCP's role tools: list, create, rename and delete roles, edit what a
role may use, and mint or revoke the service tokens that hold a role. See
:mod:`admin_actions` for why both doors call these instead of carrying
their own copies.

A role is named in four places: the saved config (``config.roles`` and
each teammate's role), the running policy, the service-token registry and
the membership rows. A rename or a delete changes them one after the
other, under the org's roles lock (``OrgRuntime.roles_lock``), which every
action that writes a role name holds: a mint here, a teammate's role
change and an accepted invitation in :mod:`user_admin_service`. Without
it, a token minted, or a teammate moved, onto a role while it is renamed
or deleted would end up on a role that no longer exists, with zero tools.

Every action that changes something runs to its end once started
(``runs_to_completion``): a cancelled request must not leave those places
disagreeing.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    SettingsConfig,
)
from mcpolis.domain.ports.service_token_repository import (
    DuplicateServiceTokenLabelError,
)
from mcpolis.domain.services.admin_actions import (
    AdminActionDeps,
    AlreadyExists,
    InvalidRequest,
    NotFound,
    publish_policy_changed,
)
from mcpolis.domain.services.audit_actions import (
    ROLE_CREATED,
    ROLE_DELETED,
    ROLE_RENAMED,
    SERVICE_TOKEN_CREATED,
    SERVICE_TOKEN_REVOKED,
    record_action,
)
from mcpolis.domain.services.cancel_shield import runs_to_completion
from mcpolis.domain.services.plan_gates import (
    assert_argument_constraints_allowed,
    assert_custom_role_capacity,
    resolve_plan,
)
from mcpolis.domain.services.service_token_service import (
    MintedServiceToken,
    ServiceTokenService,
)

# Lowercase alphanumerics, hyphen, underscore; must start with an
# alphanumeric; max 64 chars. Keeps ``svc:<label>`` clean in audit rows,
# log context, and filter UIs.
SERVICE_TOKEN_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class RoleSummary(BaseModel):
    """One role as both doors list it."""

    name: str
    is_admin: bool
    is_default: bool
    user_count: int
    service_token_count: int


class RoleAdminService:
    """Role and service-token actions for one org admin at a time."""

    def __init__(self, deps: AdminActionDeps) -> None:
        self._deps = deps

    def _tokens(self) -> ServiceTokenService:
        if self._deps.service_token_service is None:
            raise InvalidRequest("Service tokens are not configured")
        return self._deps.service_token_service

    # --- Roles ---

    async def list_roles(self, org_id: str) -> list[RoleSummary]:
        runtime = await self._deps.runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        user_counts: dict[str, int] = {}
        for user in config.users.values():
            user_counts[user.role] = user_counts.get(user.role, 0) + 1
        token_counts: dict[str, int] = {}
        if self._deps.service_token_service is not None:
            token_counts = await self._deps.service_token_service.count_by_role(
                org_id,
            )
        return [
            RoleSummary(
                name=name,
                is_admin=role.is_admin,
                is_default=role.is_default,
                user_count=user_counts.get(name, 0),
                service_token_count=token_counts.get(name, 0),
            )
            for name, role in config.roles.items()
        ]

    @runs_to_completion
    async def create_role(
        self,
        org_id: str,
        name: str,
        *,
        copy_from: str | None,
        actor: str,
        source: str,
    ) -> SettingsConfig:
        """Create a role, optionally a copy of ``copy_from``'s settings.
        ``source`` names the door for the plan-limit analytics event."""
        runtime = await self._deps.runtime_manager.get(org_id)
        # Custom-role gate: the built-in roles (admin, and the is_default
        # one) don't count.
        plan = await resolve_plan(self._deps.org_repo, org_id)
        current_custom = sum(
            1 for role in runtime.policy_engine.config.roles.values()
            if not role.is_admin and not role.is_default
        )
        assert_custom_role_capacity(
            plan, current_custom,
            source=source, org_id=org_id, actor_email=actor,
        )
        try:
            new_config = await self._deps.policy_store.create_role(
                org_id, name, copy_from=copy_from,
            )
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        runtime.policy_engine.reload(new_config)
        await record_action(
            self._deps.audit_repo, org_id,
            action=ROLE_CREATED, actor=actor, detail=name,
        )
        return new_config

    @runs_to_completion
    async def rename_role(
        self, org_id: str, role_name: str, new_name: str, *, actor: str,
    ) -> SettingsConfig:
        """Rename a role everywhere it is named.

        Holds the roles lock until the tokens and membership rows have
        moved, so no token can be minted on the old name, and no delete
        can pass its token check, mid-rename. The running policy is
        reloaded right after the saved rename, so it always matches the
        saved config; if a later write fails, renaming the role back
        repairs it."""
        runtime = await self._deps.runtime_manager.get(org_id)
        async with runtime.roles_lock:
            try:
                new_config = await self._deps.policy_store.rename_role(
                    org_id, role_name, new_name,
                )
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            runtime.policy_engine.reload(new_config)
            if self._deps.service_token_service is not None:
                await self._deps.service_token_service.rename_role(
                    org_id, role_name, new_name,
                )
            if self._deps.org_repo is not None:
                await self._deps.org_repo.rename_role(
                    org_id, role_name, new_name,
                )
        # The old name matches nobody any more; notify under the new one.
        publish_policy_changed(self._deps.event_bus, org_id, role=new_name)
        await record_action(
            self._deps.audit_repo, org_id,
            action=ROLE_RENAMED, actor=actor,
            detail=f"{role_name} → {new_name}",
        )
        return new_config

    @runs_to_completion
    async def delete_role(
        self, org_id: str, role_name: str, *, actor: str,
    ) -> None:
        """Delete a role nobody holds.

        The saved config refuses a role a teammate holds; service tokens
        live in their own registry, so they are counted here. Holds the
        roles lock from that count to the reload, so no token can be
        minted on, or renamed onto, the role in between."""
        runtime = await self._deps.runtime_manager.get(org_id)
        async with runtime.roles_lock:
            if self._deps.service_token_service is not None:
                token_counts = (
                    await self._deps.service_token_service.count_by_role(org_id)
                )
                in_use = token_counts.get(role_name, 0)
                if in_use:
                    raise InvalidRequest(
                        f"Cannot delete role '{role_name}': {in_use} "
                        "service token(s) assigned",
                    )
            try:
                new_config = await self._deps.policy_store.delete_role(
                    org_id, role_name,
                )
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            runtime.policy_engine.reload(new_config)
        publish_policy_changed(self._deps.event_bus, org_id, role=role_name)
        await record_action(
            self._deps.audit_repo, org_id,
            action=ROLE_DELETED, actor=actor, detail=role_name,
        )

    # --- What a role may use ---

    @runs_to_completion
    async def _edit(
        self,
        org_id: str,
        role_name: str,
        write: Callable[[], Awaitable[SettingsConfig]],
    ) -> SettingsConfig:
        """Save one change to a role's settings, reload the running
        policy, and tell the role's sessions to list their tools again.
        The saved config refuses a role that doesn't exist."""
        runtime = await self._deps.runtime_manager.get(org_id)
        try:
            new_config = await write()
        except ValueError as e:
            raise NotFound(str(e)) from None
        runtime.policy_engine.reload(new_config)
        publish_policy_changed(self._deps.event_bus, org_id, role=role_name)
        return new_config

    async def set_mcp_access(
        self, org_id: str, role_name: str, mcp_access: McpAccessConfig,
    ) -> SettingsConfig:
        return await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.set_role_mcp_access(
                org_id, role_name, mcp_access,
            ),
        )

    async def set_mcp_access_entry(
        self, org_id: str, role_name: str, mcp_id: str, enabled: bool,
        *, actor: str,
    ) -> SettingsConfig:
        new_config = await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.set_role_mcp_access_entry(
                org_id, role_name, mcp_id, enabled,
            ),
        )
        get_analytics().track_async(
            actor,
            "role_mcp_access_changed",
            {"role_name": role_name, "upstream_id": mcp_id, "enabled": enabled},
        )
        return new_config

    async def set_auto_enable_new(
        self, org_id: str, role_name: str, auto_enable_new: bool,
    ) -> SettingsConfig:
        return await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.set_role_auto_enable_new(
                org_id, role_name, auto_enable_new,
            ),
        )

    async def set_tool_access_entry(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        enabled: bool,
        *,
        actor: str,
    ) -> SettingsConfig:
        new_config = await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.set_role_tool_access_entry(
                org_id, role_name, upstream_id, tool_name, enabled,
            ),
        )
        get_analytics().track_async(
            actor,
            "role_tool_access_changed",
            {
                "role_name": role_name,
                "upstream_id": upstream_id,
                "tool_name": tool_name,
                "decision": "allow" if enabled else "deny",
            },
        )
        return new_config

    async def remove_tool_access_entry(
        self, org_id: str, role_name: str, upstream_id: str, tool_name: str,
    ) -> SettingsConfig:
        return await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.remove_role_tool_access_entry(
                org_id, role_name, upstream_id, tool_name,
            ),
        )

    async def set_tool_fallback_enabled(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        fallback_enabled: bool | None,
    ) -> SettingsConfig:
        return await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.set_role_tool_fallback_enabled(
                org_id, role_name, upstream_id, fallback_enabled,
            ),
        )

    async def set_category_default(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        annotation: str,
        enabled: bool,
    ) -> SettingsConfig:
        return await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.set_role_tool_category_default(
                org_id, role_name, upstream_id, annotation, enabled,
            ),
        )

    async def remove_category_default(
        self, org_id: str, role_name: str, upstream_id: str, annotation: str,
    ) -> SettingsConfig:
        return await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.remove_role_tool_category_default(
                org_id, role_name, upstream_id, annotation,
            ),
        )

    async def set_argument_constraint(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
        *,
        pattern: str,
        mode: str,
        actor: str,
        source: str,
    ) -> SettingsConfig:
        """``mode`` "allow": the argument must match ``pattern``; "forbid":
        it must not. ``source`` names the door for the plan-limit
        analytics event."""
        plan = await resolve_plan(self._deps.org_repo, org_id)
        assert_argument_constraints_allowed(
            plan, source=source, org_id=org_id, actor_email=actor,
        )
        try:
            re.compile(pattern)
        except re.error as e:
            raise InvalidRequest(f"Invalid regex: {e}") from None
        if mode == "allow" or mode == "forbid":
            constraint = ArgumentConstraint(pattern=pattern, mode=mode)
        else:
            raise InvalidRequest(
                f"Invalid mode: {mode} (must be 'allow' or 'forbid')",
            )
        return await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.set_role_argument_constraint(
                org_id, role_name, upstream_id, tool_name, arg_name,
                constraint,
            ),
        )

    async def remove_argument_constraint(
        self,
        org_id: str,
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
    ) -> SettingsConfig:
        return await self._edit(
            org_id, role_name,
            lambda: self._deps.policy_store.remove_role_argument_constraint(
                org_id, role_name, upstream_id, tool_name, arg_name,
            ),
        )

    # --- Service tokens: each holds one role by name ---

    @runs_to_completion
    async def mint_service_token(
        self, org_id: str, *, label: str, role: str, actor: str,
    ) -> MintedServiceToken:
        """Mint a token on ``role``. The raw token exists only in the
        returned value.

        Holds the roles lock from the role check to the insert, so a
        rename or delete of the role can't land in between and strand
        the new token."""
        if not SERVICE_TOKEN_LABEL.match(label):
            raise InvalidRequest(
                "Label must be 1-64 chars of lowercase letters, digits, "
                "'-' or '_', starting with a letter or digit",
            )
        tokens = self._tokens()
        runtime = await self._deps.runtime_manager.get(org_id)
        async with runtime.roles_lock:
            if role not in runtime.policy_engine.config.roles:
                raise InvalidRequest(f"Role '{role}' not found")
            try:
                minted = await tokens.mint(
                    org_id=org_id, label=label, role_name=role,
                    created_by=actor,
                )
            except DuplicateServiceTokenLabelError:
                raise AlreadyExists(
                    f"Service token '{label}' already exists",
                ) from None
        await record_action(
            self._deps.audit_repo, org_id,
            action=SERVICE_TOKEN_CREATED, actor=actor,
            detail=f"{label} (role {role})",
        )
        get_analytics().track_async(
            actor, "service_token_created", {"label": label, "role": role},
        )
        return minted

    @runs_to_completion
    async def revoke_service_token(
        self, org_id: str, label: str, *, actor: str,
    ) -> None:
        """Revoke a token: its next gateway request is refused."""
        if not await self._tokens().revoke(org_id, label):
            raise NotFound(f"Service token '{label}' not found")
        await record_action(
            self._deps.audit_repo, org_id,
            action=SERVICE_TOKEN_REVOKED, actor=actor, detail=label,
        )
        get_analytics().track_async(
            actor, "service_token_revoked", {"label": label},
        )
