from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.domain.model.settings import ArgumentConstraint, UserDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID


@pytest.mark.asyncio
async def test_ensure_defaults_creates_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    store = FileConfigStore(path)
    config = await store.ensure_defaults(DEFAULT_ORG_ID)
    assert "admin" in config.roles
    assert "user" in config.roles
    assert path.exists()


@pytest.mark.asyncio
async def test_ensure_defaults_preserves_existing(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"roles": {"custom": {"is_admin": False}}, "users": {}}))
    store = FileConfigStore(path)
    config = await store.ensure_defaults(DEFAULT_ORG_ID)
    assert "custom" in config.roles
    assert "admin" not in config.roles  # didn't overwrite


@pytest.mark.asyncio
async def test_set_and_remove_user(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)

    config = await store.set_user(DEFAULT_ORG_ID,"alice@test.com", UserDefinition(role="admin"))
    assert "alice@test.com" in config.users
    assert config.users["alice@test.com"].role == "admin"

    config = await store.remove_user(DEFAULT_ORG_ID,"alice@test.com")
    assert "alice@test.com" not in config.users


@pytest.mark.asyncio
async def test_remove_nonexistent_user_raises(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)

    with pytest.raises(ValueError, match="not found"):
        await store.remove_user(DEFAULT_ORG_ID,"nobody@test.com")


@pytest.mark.asyncio
async def test_set_user_role(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.set_user(DEFAULT_ORG_ID,"alice@test.com", UserDefinition(role="admin"))

    config = await store.set_user_role(DEFAULT_ORG_ID,"alice@test.com", "user")
    assert config.users["alice@test.com"].role == "user"


@pytest.mark.asyncio
async def test_persistence_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    store = FileConfigStore(path)
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.set_user(DEFAULT_ORG_ID,"alice@test.com", UserDefinition(role="admin"))

    # Re-load from same path
    store2 = FileConfigStore(path)
    config = await store2.load(DEFAULT_ORG_ID)
    assert "alice@test.com" in config.users
    assert config.users["alice@test.com"].role == "admin"


@pytest.mark.asyncio
async def test_create_role(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    config = await store.create_role(DEFAULT_ORG_ID,"developer")
    assert "developer" in config.roles


@pytest.mark.asyncio
async def test_create_role_with_copy_from(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.set_role_mcp_access_entry(DEFAULT_ORG_ID,"admin", "github", True)
    await store.set_role_tool_category_default(DEFAULT_ORG_ID,"admin", "github", "destructive", False)

    config = await store.create_role(DEFAULT_ORG_ID,"power-user", copy_from="admin")
    assert "power-user" in config.roles
    assert config.roles["power-user"].settings.mcp_access.mcps["github"] is True
    assert config.roles["power-user"].settings.tool_access["github"].category_defaults["destructive"] is False
    assert config.roles["power-user"].is_admin is False


@pytest.mark.asyncio
async def test_create_role_copy_from_nonexistent_raises(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    with pytest.raises(ValueError, match="not found"):
        await store.create_role(DEFAULT_ORG_ID,"dev", copy_from="nonexistent")


@pytest.mark.asyncio
async def test_create_role_duplicate_raises(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    with pytest.raises(ValueError, match="already exists"):
        await store.create_role(DEFAULT_ORG_ID,"admin")


@pytest.mark.asyncio
async def test_delete_role(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.create_role(DEFAULT_ORG_ID,"developer")
    config = await store.delete_role(DEFAULT_ORG_ID,"developer")
    assert "developer" not in config.roles


@pytest.mark.asyncio
async def test_delete_role_with_users_raises(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.create_role(DEFAULT_ORG_ID,"developer")
    await store.set_user(DEFAULT_ORG_ID,"alice@test.com", UserDefinition(role="developer"))
    with pytest.raises(ValueError, match="user"):
        await store.delete_role(DEFAULT_ORG_ID,"developer")


@pytest.mark.asyncio
async def test_rename_role(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.create_role(DEFAULT_ORG_ID,"developer")
    await store.set_user(DEFAULT_ORG_ID,"alice@test.com", UserDefinition(role="developer"))

    config = await store.rename_role(DEFAULT_ORG_ID,"developer", "dev")
    assert "dev" in config.roles
    assert "developer" not in config.roles
    assert config.users["alice@test.com"].role == "dev"


@pytest.mark.asyncio
async def test_create_mcp_access_uses_auto_enable_new(tmp_path: Path) -> None:
    """New MCPs use each role's auto_enable_new setting."""
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    # Both roles default to auto_enable_new=True, disable user
    await store.set_role_auto_enable_new(DEFAULT_ORG_ID,"user", False)

    config = await store.create_mcp_access(DEFAULT_ORG_ID,"new-mcp")

    # admin has auto_enable_new=True → enabled
    assert config.roles["admin"].settings.mcp_access.mcps["new-mcp"] is True
    # user has auto_enable_new=False → disabled
    assert config.roles["user"].settings.mcp_access.mcps["new-mcp"] is False


# --- Tool access ---


@pytest.mark.asyncio
async def test_set_role_tool_access_entry(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    config = await store.set_role_tool_access_entry(DEFAULT_ORG_ID,"admin", "github", "create_issue", True)
    tac = config.roles["admin"].settings.tool_access
    assert tac["github"].tools["create_issue"] is True


@pytest.mark.asyncio
async def test_remove_role_tool_access_entry(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.set_role_tool_access_entry(DEFAULT_ORG_ID,"admin", "github", "create_issue", True)
    config = await store.remove_role_tool_access_entry(DEFAULT_ORG_ID,"admin", "github", "create_issue")
    tac = config.roles["admin"].settings.tool_access["github"]
    assert "create_issue" not in tac.tools


@pytest.mark.asyncio
async def test_set_role_tool_fallback_enabled(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    config = await store.set_role_tool_fallback_enabled(DEFAULT_ORG_ID,"admin", "slack", False)
    tac = config.roles["admin"].settings.tool_access
    assert tac["slack"].fallback_enabled is False
    config = await store.set_role_tool_fallback_enabled(DEFAULT_ORG_ID,"admin", "slack", None)
    assert config.roles["admin"].settings.tool_access["slack"].fallback_enabled is None


@pytest.mark.asyncio
async def test_set_role_tool_category_default(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    config = await store.set_role_tool_category_default(DEFAULT_ORG_ID,"user", "db", "destructive", False)
    tac = config.roles["user"].settings.tool_access
    assert tac["db"].category_defaults["destructive"] is False


@pytest.mark.asyncio
async def test_remove_role_tool_category_default(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.set_role_tool_category_default(DEFAULT_ORG_ID,"user", "db", "destructive", False)
    config = await store.remove_role_tool_category_default(DEFAULT_ORG_ID,"user", "db", "destructive")
    assert "destructive" not in config.roles["user"].settings.tool_access["db"].category_defaults


# --- Upstream options ---


@pytest.mark.asyncio
async def test_set_and_remove_upstream_options(tmp_path: Path) -> None:
    from mcpolis.domain.model.settings import UpstreamOptions

    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)

    options = UpstreamOptions(display_name="GitHub", auth_mode="service_account")
    config = await store.set_upstream_options(DEFAULT_ORG_ID, "github", options)
    assert "github" in config.upstreams
    assert config.upstreams["github"].display_name == "GitHub"

    config = await store.remove_upstream_options(DEFAULT_ORG_ID, "github")
    assert "github" not in config.upstreams


# --- Argument constraints ---


@pytest.mark.asyncio
async def test_set_role_argument_constraint(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    constraint = ArgumentConstraint(pattern=r"^SELECT\s", mode="allow")
    config = await store.set_role_argument_constraint(DEFAULT_ORG_ID,
        "admin", "database", "query", "sql", constraint
    )
    saved = config.roles["admin"].settings.argument_constraints["database__query"]["sql"]
    assert saved.pattern == r"^SELECT\s"
    assert saved.mode == "allow"


@pytest.mark.asyncio
async def test_set_role_argument_constraint_invalid_role(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    constraint = ArgumentConstraint(pattern=r"^SELECT", mode="allow")
    with pytest.raises(ValueError, match="not found"):
        await store.set_role_argument_constraint(DEFAULT_ORG_ID,
            "nonexistent", "database", "query", "sql", constraint
        )


@pytest.mark.asyncio
async def test_remove_role_argument_constraint(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    constraint = ArgumentConstraint(pattern=r"^SELECT", mode="allow")
    await store.set_role_argument_constraint(DEFAULT_ORG_ID,
        "admin", "database", "query", "sql", constraint
    )
    config = await store.remove_role_argument_constraint(DEFAULT_ORG_ID,
        "admin", "database", "query", "sql"
    )
    assert "database__query" not in config.roles["admin"].settings.argument_constraints


@pytest.mark.asyncio
async def test_remove_role_argument_constraint_keeps_siblings(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.ensure_defaults(DEFAULT_ORG_ID)
    await store.set_role_argument_constraint(DEFAULT_ORG_ID,
        "admin", "database", "query", "sql",
        ArgumentConstraint(pattern=r"^SELECT", mode="allow"),
    )
    await store.set_role_argument_constraint(DEFAULT_ORG_ID,
        "admin", "database", "query", "limit",
        ArgumentConstraint(pattern=r"^\d+$", mode="allow"),
    )
    config = await store.remove_role_argument_constraint(DEFAULT_ORG_ID,
        "admin", "database", "query", "sql"
    )
    constraints = config.roles["admin"].settings.argument_constraints
    assert "database__query" in constraints
    assert "sql" not in constraints["database__query"]
    assert constraints["database__query"]["limit"].pattern == r"^\d+$"
