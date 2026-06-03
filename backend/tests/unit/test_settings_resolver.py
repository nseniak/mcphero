from __future__ import annotations

from mcpolis.domain.model.settings import (
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    ToolAccessConfig,
    UserDefinition,
)

from mcpolis.domain.services.settings_resolver import resolve_settings


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
