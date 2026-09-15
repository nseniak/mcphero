from __future__ import annotations

from mcpolis.domain.model.settings import (
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    ToolAccessConfig,
    UserDefinition,
)

from mcpolis.domain.services.settings_resolver import (
    admin_emails,
    resolve_settings,
    would_remove_last_admin,
)


def test_unknown_user_gets_empty_settings() -> None:
    config = SettingsConfig(
        roles={"admin": RoleDefinition()},
        users={},
    )
    resolved = resolve_settings(config, "nobody@test.com")
    assert resolved.role_name == ""
    assert resolved.mcp_access == McpAccessConfig()
    assert resolved.is_admin is False


def test_simple_role_resolution() -> None:
    config = SettingsConfig(
        roles={
            "admin": RoleDefinition(
                is_admin=True,
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={"github": True}),
                ),
            ),
        },
        users={"alice@test.com": UserDefinition(role="admin")},
    )
    resolved = resolve_settings(config, "alice@test.com")
    assert resolved.role_name == "admin"
    assert resolved.mcp_access.mcps == {"github": True}
    assert resolved.is_admin is True


def test_role_not_found_gets_empty() -> None:
    config = SettingsConfig(
        roles={"admin": RoleDefinition()},
        users={"alice@test.com": UserDefinition(role="nonexistent")},
    )
    resolved = resolve_settings(config, "alice@test.com")
    assert resolved.role_name == ""
    assert resolved.is_admin is False


def test_tool_access_resolved() -> None:
    config = SettingsConfig(
        roles={
            "user": RoleDefinition(
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={"slack": True}),
                    tool_access={"slack": ToolAccessConfig(
                        fallback_enabled=False,
                        tools={"read_messages": True, "send_message": False},
                    )},
                ),
            ),
        },
        users={"alice@test.com": UserDefinition(role="user")},
    )
    resolved = resolve_settings(config, "alice@test.com")
    assert "slack" in resolved.tool_access
    config_resolved = resolved.tool_access["slack"]
    assert config_resolved.tools["read_messages"] is True
    assert config_resolved.tools["send_message"] is False
    assert config_resolved.fallback_enabled is False


def test_auto_enable_new_preserved() -> None:
    """auto_enable_new on McpAccessConfig is a simple bool field."""
    config = McpAccessConfig.model_validate({
        "auto_enable_new": True,
        "mcps": {"github": True},
    })
    assert config.auto_enable_new is True
    assert config.mcps == {"github": True}


# --- Last-admin guard ------------------------------------------------
# The org's one unrecoverable state: no user holds an admin role, so
# nothing in the product can restore access. Both mutation doors
# (dashboard routes, admin MCP tools) call these.


def make_org_config(
    users: dict[str, str],
    *,
    admin_roles: tuple[str, ...] = ("admin",),
    plain_roles: tuple[str, ...] = ("developer",),
) -> SettingsConfig:
    """Build a SettingsConfig from ``{email: role_name}``.

    Role names listed in ``admin_roles`` get ``is_admin=True``. A role
    a user references but that appears in neither tuple is deliberately
    absent from ``config.roles`` — that models a deleted role.
    """
    roles = {name: RoleDefinition(is_admin=True) for name in admin_roles}
    roles.update({name: RoleDefinition() for name in plain_roles})
    return SettingsConfig(
        roles=roles,
        users={email: UserDefinition(role=r) for email, r in users.items()},
    )


def test_admin_emails_lists_only_admin_roles() -> None:
    config = make_org_config({
        "boss@test.com": "admin",
        "dev@test.com": "developer",
    })
    assert admin_emails(config) == {"boss@test.com"}


def test_admin_emails_ignores_a_user_whose_role_was_deleted() -> None:
    # "ghost" is in no role tuple, so config.roles has no such entry.
    # Fail closed: a dangling role grants nothing, admin included.
    config = make_org_config({"orphan@test.com": "ghost"})
    assert admin_emails(config) == set()


def test_removing_the_only_admin_is_refused() -> None:
    config = make_org_config({
        "boss@test.com": "admin",
        "dev@test.com": "developer",
    })
    assert would_remove_last_admin(config, "boss@test.com") is True


def test_removing_one_of_two_admins_is_allowed() -> None:
    config = make_org_config({
        "boss@test.com": "admin",
        "deputy@test.com": "admin",
    })
    assert would_remove_last_admin(config, "boss@test.com") is False


def test_removing_a_non_admin_is_allowed() -> None:
    config = make_org_config({
        "boss@test.com": "admin",
        "dev@test.com": "developer",
    })
    assert would_remove_last_admin(config, "dev@test.com") is False


def test_removing_an_unknown_email_is_allowed() -> None:
    # Not a member at all — the count cannot change. The route's own
    # 404 handling owns this case; the guard must not mask it.
    config = make_org_config({"boss@test.com": "admin"})
    assert would_remove_last_admin(config, "stranger@test.com") is False


def test_demoting_the_only_admin_is_refused() -> None:
    config = make_org_config({
        "boss@test.com": "admin",
        "dev@test.com": "developer",
    })
    assert would_remove_last_admin(
        config, "boss@test.com", new_role="developer",
    ) is True


def test_moving_the_only_admin_to_another_admin_role_is_allowed() -> None:
    # Two distinct role names, both is_admin. The org keeps an admin,
    # so this must pass — guarding on role *name* instead of the
    # is_admin flag would wrongly block it.
    config = make_org_config(
        {"boss@test.com": "admin"},
        admin_roles=("admin", "owner"),
    )
    assert would_remove_last_admin(
        config, "boss@test.com", new_role="owner",
    ) is False


def test_demoting_the_only_admin_to_a_deleted_role_is_refused() -> None:
    # The target role does not exist, so it grants nothing. The org
    # would end up with no admin.
    config = make_org_config({"boss@test.com": "admin"})
    assert would_remove_last_admin(
        config, "boss@test.com", new_role="ghost",
    ) is True


def test_promoting_a_non_admin_is_allowed() -> None:
    config = make_org_config({
        "boss@test.com": "admin",
        "dev@test.com": "developer",
    })
    assert would_remove_last_admin(
        config, "dev@test.com", new_role="admin",
    ) is False


def test_an_org_with_no_admin_at_all_does_not_block_further_edits() -> None:
    # Already broken (only a service operator can repair it). The guard
    # must not also freeze the org's member list on top of that.
    config = make_org_config(
        {"dev@test.com": "developer"}, admin_roles=(),
    )
    assert would_remove_last_admin(config, "dev@test.com") is False


# --- "eligible" narrowing: invited but never signed in ----------------
# A membership row appears on first sign-in. An address that only ever
# received an invitation can never administer anything, so counting it
# as an admin would let one typo'd invitation stand in for a real admin
# and lock the org out — the exact failure this guard exists to stop.


def test_admin_emails_ignores_an_admin_who_never_signed_in() -> None:
    config = make_org_config({
        "boss@test.com": "admin",
        "tpyo@test.com": "admin",
    })
    assert admin_emails(config, eligible={"boss@test.com"}) == {"boss@test.com"}


def test_removing_the_only_signed_in_admin_is_refused() -> None:
    # The org looks like it has two admins. Only one of them exists.
    config = make_org_config({
        "boss@test.com": "admin",
        "tpyo@test.com": "admin",
    })
    assert would_remove_last_admin(
        config, "boss@test.com", eligible={"boss@test.com"},
    ) is True


def test_removing_an_invited_admin_who_never_signed_in_is_allowed() -> None:
    # Cleaning up the typo must stay possible.
    config = make_org_config({
        "boss@test.com": "admin",
        "tpyo@test.com": "admin",
    })
    assert would_remove_last_admin(
        config, "tpyo@test.com", eligible={"boss@test.com"},
    ) is False


def test_handover_needs_the_successor_to_have_signed_in() -> None:
    config = make_org_config({
        "boss@test.com": "admin",
        "heir@test.com": "admin",
    })
    # Successor invited but not yet signed in: the handover is refused.
    assert would_remove_last_admin(
        config, "boss@test.com", eligible={"boss@test.com"},
    ) is True
    # Once they sign in, it goes through.
    assert would_remove_last_admin(
        config, "boss@test.com",
        eligible={"boss@test.com", "heir@test.com"},
    ) is False


def test_eligible_none_counts_everyone() -> None:
    # The stores call the guard without membership data — they enforce
    # the coarser config-level invariant atomically. Passing no
    # eligible set must keep that older behaviour exactly.
    config = make_org_config({
        "boss@test.com": "admin",
        "tpyo@test.com": "admin",
    })
    assert would_remove_last_admin(config, "boss@test.com") is False
