"""Removing an upstream MCP deletes its role-level rules in every role,
and adding one never inherits rules stored under the same id.

docs/upstream-mcps.md promises that removing an MCP "drops any per-tool
overrides or argument checks attached to it (in roles)". Before the fix
the rules survived the remove, so re-adding a server under the same id
silently brought back the old tool denials and argument checks.

Every removal goes through ``UpstreamConfigService.remove_upstream`` and
every new upstream gets its role access from ``grant_role_access``, so
the tests pin those two steps, the Admin MCP round trip through the
shared admin actions, and the runtime manager's wiring.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.adapters.repositories.file_sandbox_file_repository import (
    FileSandboxFileRepository,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.repositories.file_tool_catalog_store import (
    FileToolCatalogStore,
)
from mcpolis.adapters.repositories.file_upstream_config_store import (
    FileUpstreamConfigStore,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONFIG,
    COLL_UPSTREAMS,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_config_repository import (
    MongoConfigRepository,
)
from mcpolis.adapters.repositories.mongo_upstream_config_repository import (
    MongoUpstreamConfigRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    ToolAccessConfig,
    UserDefinition,
)
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.config_repository import ConfigRepository
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_config_service import UpstreamConfigService
from mcpolis.domain.services.upstream_role_rules import remove_upstream_role_rules
from mcpolis.entrypoints.controllers.admin_mcp_controller import (
    create_admin_mcp_server,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_user_id,
)
from tests.unit.factories import make_runtime_manager
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database

ADMIN_EMAIL = "admin@example.com"
MEMBER_EMAIL = "member@example.com"
GITHUB_URL = "https://github.example.invalid/mcp"
# What create_mcp_access gives every role for a new server.
FRESH_TOOL_ACCESS = ToolAccessConfig(
    fallback_enabled=True,
    category_defaults={"readOnly": True, "destructive": True},
)
# Loopback, as in the suite's own Admin MCP tests: the add path resolves
# the host, and run-unit-tests.sh lets loopback through.
GITHUB_ARGS: dict[str, Any] = {
    "mcp_id": "github",
    "display_name": "GitHub",
    "transport": "streamable_http",
    "url": "http://localhost:9000/mcp",
}
# Visible skip, not a silently shorter list, when Mongo is down.
MONGO_ONLY = pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
BACKENDS: list[object] = ["file", pytest.param("mongo", marks=MONGO_ONLY)]


# ---------- builders ----------


def make_role_rules_config() -> SettingsConfig:
    """Two roles with rules on ``github`` plus rules that must survive its
    removal: ``slack``'s, the entries of server ``github__x``, and a
    bare-tool-name check. The key ``github__x__run`` is github's: the
    gateway splits a tool name at its first ``__``."""
    def make_role() -> RoleDefinition:
        role = RoleDefinition()
        s = role.settings
        s.mcp_access.mcps = {"github": False, "slack": True, "github__x": True}
        s.tool_access = {
            "github": ToolAccessConfig(tools={"delete_repo": False}),
            "slack": ToolAccessConfig(tools={"post": False}),
            "github__x": ToolAccessConfig(tools={"run": False}),
        }
        s.argument_constraints = {
            "github__create_issue": {"repo": ArgumentConstraint(pattern="^acme/")},
            "github__x__run": {"cmd": ArgumentConstraint(pattern="^ls$")},
            "slack__post": {"channel": ArgumentConstraint(pattern="^#ops$")},
            # Bare tool name: applies to any server, not owned by github.
            "create_issue": {"title": ArgumentConstraint(pattern=".+")},
        }
        return role

    return SettingsConfig(
        roles={"admin": make_role(), "user": make_role()},
    )


def assert_github_rules_gone_and_others_kept(config: SettingsConfig) -> None:
    for role in config.roles.values():
        s = role.settings
        assert s.mcp_access.mcps == {"slack": True, "github__x": True}
        assert set(s.tool_access) == {"slack", "github__x"}
        assert set(s.argument_constraints) == {"slack__post", "create_issue"}


def make_roles_json(*, orphan_github_rules: bool) -> dict[str, Any]:
    """config.json content. With *orphan_github_rules*, the "user" role
    still carries rules for "github" although no github server exists:
    what every remove made before the fix left behind."""
    user_settings: dict[str, Any] = {"mcp_access": {"auto_enable_new": True}}
    if orphan_github_rules:
        user_settings["mcp_access"]["mcps"] = {"github": False}
        user_settings["tool_access"] = {
            "github": {
                "fallback_enabled": True,
                "category_defaults": {"readOnly": True, "destructive": True},
                "tools": {"delete_repo": False},
            },
        }
        user_settings["argument_constraints"] = {
            "github__create_issue": {"repo": {"pattern": "^acme/"}},
        }
    return {
        "roles": {
            "admin": {"is_admin": True,
                      "settings": {"mcp_access": {"auto_enable_new": True}}},
            "user": {"is_default": True, "settings": user_settings},
        },
        "users": {
            ADMIN_EMAIL: {"role": "admin"},
            MEMBER_EMAIL: {"role": "user"},
        },
    }


def make_github() -> UpstreamDefinition:
    return UpstreamDefinition(
        id="github",
        display_name="GitHub",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=GITHUB_URL),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


def make_token() -> OAuthToken:
    return OAuthToken(
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(UTC),
        scopes=["read"],
        refresh_token_created_at=datetime.now(UTC),
    )


@asynccontextmanager
async def make_config_repo(
    backend: str, tmp_path: Path,
) -> AsyncIterator[ConfigRepository]:
    if backend == "file":
        yield FileConfigStore(tmp_path / "config.json")
        return
    async with temp_mongo_database() as db:
        yield MongoConfigRepository(
            OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG),
        )


async def make_admin_server(
    tmp_path: Path,
    *,
    orphan_github_rules: bool = False,
    client_manager: UpstreamClientManager | None = None,
) -> tuple[Any, FileConfigStore, PolicyEngine]:
    """Real Admin MCP server on file storage, wired like ``OrgRuntimeManager``."""
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    (tmp_path / "config.json").write_text(
        json.dumps(make_roles_json(orphan_github_rules=orphan_github_rules)),
    )
    config_store = FileConfigStore(tmp_path / "config.json")
    policy_engine = PolicyEngine(config_store.ensure_defaults_sync(DEFAULT_ORG_ID))
    template_var_repo = FileTemplateVarRepository(tmp_path / "data")
    client_manager = client_manager or UpstreamClientManager(
        [], template_var_repo=template_var_repo,
    )
    tool_registry = ToolRegistry([], client_manager)
    connection_store = FileConnectionStore(tmp_path)
    config_service = UpstreamConfigService(
        FileUpstreamConfigStore(McpJsonStore(tmp_path / "mcp.json"), config_store),
        client_manager,
        tool_registry,
        connection_store,
        config_repo=config_store,
        policy_engine=policy_engine,
        template_var_repo=template_var_repo,
    )
    org_repo = FileOrganizationRepository(tmp_path / "data")
    await org_repo.update_subscription(
        DEFAULT_ORG_ID, Subscription(plan=PlanName.team),
    )
    server = create_admin_mcp_server(
        runtime_manager=make_runtime_manager(
            policy_engine,
            tool_registry=tool_registry,
            client_manager=client_manager,
            config_service=config_service,
        ),
        audit_repo=FileAuditRepository(tmp_path / "data" / "audit.jsonl"),
        policy_store=config_store,
        template_var_repo=template_var_repo,
        connection_store=connection_store,
        org_repo=org_repo,
    )
    return server, config_store, policy_engine


async def call_admin_tool(server: Any, name: str, args: dict[str, Any]) -> str:
    org_token = current_org_id.set(DEFAULT_ORG_ID)
    user_token = current_user_id.set(ADMIN_EMAIL)
    try:
        result: Any = await server.call_tool(name, args)
    finally:
        current_org_id.reset(org_token)
        current_user_id.reset(user_token)
    return str(result[0][0].text)


def assert_member_may_call(
    policy_engine: PolicyEngine, calls: list[tuple[str, dict[str, object]]],
) -> None:
    for tool, args in calls:
        decision = policy_engine.decide_tool_call(MEMBER_EMAIL, "github", tool, args)
        assert decision.allowed, f"{tool}: {decision.reason}"


def make_file_service(
    tmp_path: Path, *, connection_store: FileConnectionStore | None = None,
) -> tuple[UpstreamConfigService, FileConfigStore, PolicyEngine]:
    """The shared add / remove step on file storage, no door in front.
    Role "admin" auto-enables new servers; role "user" does not."""
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    roles = make_roles_json(orphan_github_rules=False)
    roles["roles"]["user"]["settings"]["mcp_access"]["auto_enable_new"] = False
    (tmp_path / "config.json").write_text(json.dumps(roles))
    config_store = FileConfigStore(tmp_path / "config.json")
    policy_engine = PolicyEngine(config_store.ensure_defaults_sync(DEFAULT_ORG_ID))
    client_manager = UpstreamClientManager([])
    service = UpstreamConfigService(
        FileUpstreamConfigStore(McpJsonStore(tmp_path / "mcp.json"), config_store),
        client_manager,
        ToolRegistry([], client_manager),
        connection_store or FileConnectionStore(tmp_path),
        config_repo=config_store,
        policy_engine=policy_engine,
    )
    return service, config_store, policy_engine


class GatedConnectionStore(FileConnectionStore):
    """Connection store whose per-upstream purge waits until released,
    holding a removal in its slow middle part."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def delete_all_for_upstream(self, org_id: str, upstream_id: str) -> int:
        self.entered.set()
        await self.release.wait()
        return await super().delete_all_for_upstream(org_id, upstream_id)


class FailingRoleRulesConfigStore(FileConfigStore):
    """Config store whose role-rule write fails, the way a Mongo write
    can fail for a moment."""

    async def remove_upstream_role_rules(
        self, org_id: str, upstream_id: str,
    ) -> SettingsConfig:
        raise ConnectionError("simulated storage outage on the config write")


# ---------- the shared removal function ----------


def test_remove_upstream_role_rules_drops_only_that_server() -> None:
    config = make_role_rules_config()
    remove_upstream_role_rules(config, "github")
    assert_github_rules_gone_and_others_kept(config)


def test_remove_upstream_role_rules_reports_whether_anything_changed() -> None:
    config = make_role_rules_config()
    assert remove_upstream_role_rules(config, "github")
    assert not remove_upstream_role_rules(config, "github")


def test_argument_check_goes_with_the_server_the_gateway_applies_it_to() -> None:
    """``github__x__run`` is the key of server github's tool ``x__run``
    AND of server ``github__x``'s tool ``run``. The gateway reads it as
    github's, so removing ``github__x`` must keep it and removing
    ``github`` must drop it."""
    config = SettingsConfig(
        roles={"user": RoleDefinition(settings=RoleSettings(
            mcp_access=McpAccessConfig(mcps={"github": True, "github__x": True}),
            argument_constraints={
                "github__x__run": {"cmd": ArgumentConstraint(pattern="^ls$")},
            },
        ))},
        users={MEMBER_EMAIL: UserDefinition(role="user")},
    )
    engine = PolicyEngine(config)

    def rm_rf_allowed() -> bool:
        return engine.decide_tool_call(
            MEMBER_EMAIL, "github", "x__run", {"cmd": "rm -rf /"},
        ).allowed

    assert not rm_rf_allowed()
    remove_upstream_role_rules(config, "github__x")
    engine.reload(config)
    assert not rm_rf_allowed()
    assert "github__x" not in config.roles["user"].settings.mcp_access.mcps

    remove_upstream_role_rules(config, "github")
    assert config.roles["user"].settings.argument_constraints == {}


# ---------- both storage modes ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_config_repo_removes_upstream_role_rules(
    backend: str, tmp_path: Path,
) -> None:
    async with make_config_repo(backend, tmp_path) as repo:
        await repo.save(DEFAULT_ORG_ID, make_role_rules_config())
        returned = await repo.remove_upstream_role_rules(DEFAULT_ORG_ID, "github")
        assert_github_rules_gone_and_others_kept(returned)
        assert_github_rules_gone_and_others_kept(await repo.load(DEFAULT_ORG_ID))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_create_mcp_access_drops_rules_left_under_the_id(
    backend: str, tmp_path: Path,
) -> None:
    async with make_config_repo(backend, tmp_path) as repo:
        await repo.save(DEFAULT_ORG_ID, make_role_rules_config())
        returned = await repo.create_mcp_access(DEFAULT_ORG_ID, "github")
        for config in (returned, await repo.load(DEFAULT_ORG_ID)):
            for role in config.roles.values():
                s = role.settings
                assert s.tool_access["github"] == FRESH_TOOL_ACCESS
                assert s.mcp_access.mcps["github"] is s.mcp_access.auto_enable_new
                assert set(s.argument_constraints) == {"slack__post", "create_issue"}
                assert s.tool_access["slack"].tools == {"post": False}


# ---------- the whole round trip through the Admin MCP ----------


@pytest.mark.asyncio
async def test_remove_then_readd_same_id_starts_with_no_role_rules(
    tmp_path: Path,
) -> None:
    server, config_store, policy_engine = await make_admin_server(tmp_path)

    assert "added" in await call_admin_tool(server, "add_upstream", GITHUB_ARGS)
    await call_admin_tool(server, "set_role_tool_access", {
        "role_name": "user", "upstream_id": "github",
        "tool_name": "delete_repo", "enabled": False,
    })
    await config_store.set_role_argument_constraint(
        DEFAULT_ORG_ID, "user", "github", "create_issue", "repo",
        ArgumentConstraint(pattern="^acme/"),
    )
    before = (await config_store.load(DEFAULT_ORG_ID)).roles["user"].settings
    assert before.tool_access["github"].tools == {"delete_repo": False}
    assert "github__create_issue" in before.argument_constraints

    removed = await call_admin_tool(server, "remove_upstream", {"mcp_id": "github"})
    assert "removed" in removed

    # The rules are gone right after the remove, in storage and in the
    # live policy the gateway checks calls against.
    for config in (await config_store.load(DEFAULT_ORG_ID), policy_engine.config):
        for role in config.roles.values():
            assert "github" not in role.settings.tool_access
            assert "github" not in role.settings.mcp_access.mcps
            assert "github__create_issue" not in role.settings.argument_constraints

    assert "added" in await call_admin_tool(server, "add_upstream", GITHUB_ARGS)
    after = (await config_store.load(DEFAULT_ORG_ID)).roles["user"].settings
    assert after.tool_access["github"] == FRESH_TOOL_ACCESS
    assert "github__create_issue" not in after.argument_constraints


@pytest.mark.asyncio
async def test_rule_written_for_a_removed_server_is_not_inherited_by_a_readd(
    tmp_path: Path,
) -> None:
    """Nothing refuses a role rule for a server that no longer exists (a
    stale Access tab, a queued Admin MCP call). That write creates a tool
    access entry with no catch-all; inherited, it would deny every tool
    of the re-added server except the one listed."""
    server, config_store, policy_engine = await make_admin_server(tmp_path)
    assert "added" in await call_admin_tool(server, "add_upstream", GITHUB_ARGS)
    assert "removed" in await call_admin_tool(
        server, "remove_upstream", {"mcp_id": "github"},
    )
    await call_admin_tool(server, "set_role_tool_access", {
        "role_name": "user", "upstream_id": "github",
        "tool_name": "delete_repo", "enabled": False,
    })
    await config_store.set_role_argument_constraint(
        DEFAULT_ORG_ID, "user", "github", "create_issue", "repo",
        ArgumentConstraint(pattern="^acme/"),
    )

    assert "added" in await call_admin_tool(server, "add_upstream", GITHUB_ARGS)

    after = (await config_store.load(DEFAULT_ORG_ID)).roles["user"].settings
    assert after.tool_access["github"] == FRESH_TOOL_ACCESS
    assert "github__create_issue" not in after.argument_constraints
    assert_member_may_call(policy_engine, [
        ("list_issues", {}),
        ("delete_repo", {}),
        ("create_issue", {"repo": "other/repo"}),
    ])


@pytest.mark.asyncio
async def test_readd_ignores_rules_orphaned_by_an_earlier_remove(
    tmp_path: Path,
) -> None:
    """Rules left by removes made before the fix are still stored. A
    re-add of that id must not inherit them."""
    server, config_store, policy_engine = await make_admin_server(
        tmp_path, orphan_github_rules=True,
    )
    assert "added" in await call_admin_tool(server, "add_upstream", GITHUB_ARGS)

    assert_member_may_call(policy_engine, [
        ("delete_repo", {}), ("create_issue", {"repo": "other/repo"}),
    ])
    after = (await config_store.load(DEFAULT_ORG_ID)).roles["user"].settings
    assert after.tool_access["github"] == FRESH_TOOL_ACCESS
    assert "github__create_issue" not in after.argument_constraints


# ---------- a failing rule purge must not undo the rest of the remove ----------


@pytest.mark.asyncio
async def test_failed_role_rule_purge_still_unregisters_and_purges(
    tmp_path: Path,
) -> None:
    """The rule purge runs last and never raises: the server is already
    removed, so a storage error there must neither skip the live
    unregister and the token / Variable purges nor turn the remove into
    an error the admin can't retry (the server is no longer listed)."""
    (tmp_path / "mcp.json").write_text(json.dumps({
        "mcpServers": {"github": {"url": GITHUB_URL}},
    }))
    (tmp_path / "config.json").write_text(
        json.dumps(make_roles_json(orphan_github_rules=False)),
    )
    config_store = FailingRoleRulesConfigStore(tmp_path / "config.json")
    upstream_store = FileUpstreamConfigStore(
        McpJsonStore(tmp_path / "mcp.json"), config_store,
    )
    github = make_github()
    client_manager = UpstreamClientManager([github])
    tool_registry = ToolRegistry([github], client_manager)
    connection_store = FileConnectionStore(tmp_path)
    await connection_store.put_admin_token(
        DEFAULT_ORG_ID, "github", make_token(), authorized_by=ADMIN_EMAIL,
    )
    template_var_repo = FileTemplateVarRepository(tmp_path / "data")
    await template_var_repo.set(DEFAULT_ORG_ID, "github", "API_KEY", "s3cr3t-value")
    service = UpstreamConfigService(
        upstream_store,
        client_manager,
        tool_registry,
        connection_store,
        template_var_repo=template_var_repo,
        sandbox_file_repo=FileSandboxFileRepository(tmp_path / "data"),
        config_repo=config_store,
        policy_engine=PolicyEngine(config_store.ensure_defaults_sync(DEFAULT_ORG_ID)),
    )

    await service.remove_upstream(DEFAULT_ORG_ID, "github")

    assert await upstream_store.get(DEFAULT_ORG_ID, "github") is None
    assert "github" not in client_manager.all_upstream_ids
    assert "github" not in tool_registry.get_upstream_ids()
    assert await connection_store.get_admin_token(DEFAULT_ORG_ID, "github") is None
    assert await template_var_repo.list_summaries(DEFAULT_ORG_ID, "github") == []


# ---------- cloud mode, through the service ----------


@pytest.mark.asyncio
@MONGO_ONLY
async def test_cloud_remove_through_the_service_drops_role_rules(
    tmp_path: Path,
) -> None:
    async with temp_mongo_database() as db:
        config_repo = MongoConfigRepository(
            OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG),
        )
        upstream_repo = MongoUpstreamConfigRepository(
            OrgScopedCollection(db[COLL_UPSTREAMS], COLL_UPSTREAMS),
            OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG),
        )
        org = "org-cloud"
        await config_repo.save(org, SettingsConfig.model_validate(
            make_roles_json(orphan_github_rules=True),
        ))
        await upstream_repo.add(org, make_github())
        github = make_github()
        client_manager = UpstreamClientManager([github])
        policy_engine = PolicyEngine(await config_repo.load(org))
        service = UpstreamConfigService(
            upstream_repo,
            client_manager,
            ToolRegistry([github], client_manager),
            FileConnectionStore(tmp_path),
            config_repo=config_repo,
            policy_engine=policy_engine,
        )

        await service.remove_upstream(org, "github")

        for config in (await config_repo.load(org), policy_engine.config):
            user = config.roles["user"].settings
            assert "github" not in user.mcp_access.mcps
            assert "github" not in user.tool_access
            assert "github__create_issue" not in user.argument_constraints


@pytest.mark.asyncio
@MONGO_ONLY
async def test_cloud_remove_survives_an_unreadable_other_upstream(
    tmp_path: Path,
) -> None:
    """One unreadable document for a DIFFERENT upstream must not break
    removing github: the remove reads nothing but github's own record."""
    encryptor = FieldEncryptor.from_master_secret("review-secret")
    async with temp_mongo_database() as db:
        config_repo = MongoConfigRepository(
            OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG, encryptor=encryptor),
        )
        upstream_repo = MongoUpstreamConfigRepository(
            OrgScopedCollection(
                db[COLL_UPSTREAMS], COLL_UPSTREAMS, encryptor=encryptor,
            ),
            OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG, encryptor=encryptor),
        )
        org = "org-cloud"
        await config_repo.save(org, SettingsConfig.model_validate(
            make_roles_json(orphan_github_rules=True),
        ))
        await upstream_repo.add(org, make_github())
        await db[COLL_UPSTREAMS].insert_one({
            "org_id": org,
            "upstream_id": "broken",
            "server_config_encrypted": "written-without-encryption",
            "options_encrypted": "written-without-encryption",
        })
        github = make_github()
        client_manager = UpstreamClientManager([github])
        tool_registry = ToolRegistry([github], client_manager)
        service = UpstreamConfigService(
            upstream_repo,
            client_manager,
            tool_registry,
            FileConnectionStore(tmp_path),
            config_repo=config_repo,
            policy_engine=PolicyEngine(await config_repo.load(org)),
        )

        await service.remove_upstream(org, "github")

        assert await upstream_repo.get(org, "github") is None
        assert "github" not in tool_registry.get_upstream_ids()
        assert "github" not in client_manager.all_upstream_ids
        user = (await config_repo.load(org)).roles["user"].settings
        assert "github" not in user.tool_access


# ---------- the runtime manager's own wiring ----------


@pytest.mark.asyncio
async def test_runtime_built_by_the_manager_drops_role_rules_on_remove(
    tmp_path: Path,
) -> None:
    """Pins the OrgRuntimeManager wiring: the round trip above wires the
    service by hand, so without this only the e2e spec would notice the
    manager no longer passing config_repo / policy_engine."""
    (tmp_path / "mcp.json").write_text(json.dumps({
        "mcpServers": {"github": {"url": GITHUB_URL}},
    }))
    (tmp_path / "config.json").write_text(
        json.dumps(make_roles_json(orphan_github_rules=True)),
    )
    config_store = FileConfigStore(tmp_path / "config.json")
    upstream_store = FileUpstreamConfigStore(
        McpJsonStore(tmp_path / "mcp.json"), config_store,
    )
    manager = OrgRuntimeManager(
        config_repo=config_store,
        upstream_config_repo=upstream_store,
        connection_repo=FileConnectionStore(tmp_path),
        audit_repo=FileAuditRepository(tmp_path / "audit.jsonl"),
        tool_catalog_repo=FileToolCatalogStore(tmp_path),
        server_url="http://localhost:8080",
    )
    runtime = manager.create_runtime_sync(
        DEFAULT_ORG_ID,
        await config_store.load(DEFAULT_ORG_ID),
        await upstream_store.get_all(DEFAULT_ORG_ID),
    )

    await runtime.config_service.remove_upstream(DEFAULT_ORG_ID, "github")

    for config in (
        await config_store.load(DEFAULT_ORG_ID), runtime.policy_engine.config,
    ):
        user = config.roles["user"].settings
        assert "github" not in user.mcp_access.mcps
        assert "github" not in user.tool_access
        assert "github__create_issue" not in user.argument_constraints


# ---------- role access for a new server ----------


@pytest.mark.asyncio
async def test_grant_role_access_gives_every_role_fresh_access_and_reloads(
    tmp_path: Path,
) -> None:
    """The shared admin add and import, and the dev demo seed, all give a
    new server its role access through this one step."""
    service, config_store, policy_engine = make_file_service(tmp_path)
    await service.add_upstream(DEFAULT_ORG_ID, make_github())

    await service.grant_role_access(DEFAULT_ORG_ID, "github")

    for config in (await config_store.load(DEFAULT_ORG_ID), policy_engine.config):
        admin = config.roles["admin"].settings
        user = config.roles["user"].settings
        assert admin.mcp_access.mcps["github"] is True
        assert user.mcp_access.mcps["github"] is False
        assert admin.tool_access["github"] == FRESH_TOOL_ACCESS
        assert user.tool_access["github"] == FRESH_TOOL_ACCESS


@pytest.mark.asyncio
async def test_adding_an_id_already_in_use_keeps_its_rules(tmp_path: Path) -> None:
    """The duplicate is refused before the role setup runs, so a failed
    add can't reset the existing server's rules."""
    server, config_store, policy_engine = await make_admin_server(tmp_path)
    assert "added" in await call_admin_tool(server, "add_upstream", GITHUB_ARGS)
    await call_admin_tool(server, "set_role_tool_access", {
        "role_name": "user", "upstream_id": "github",
        "tool_name": "delete_repo", "enabled": False,
    })

    again = await call_admin_tool(server, "add_upstream", GITHUB_ARGS)

    assert "added" not in again
    for config in (await config_store.load(DEFAULT_ORG_ID), policy_engine.config):
        assert config.roles["user"].settings.tool_access["github"].tools == {
            "delete_repo": False,
        }


# ---------- a re-add racing a removal ----------


@pytest.mark.asyncio
async def test_readd_during_a_slow_remove_keeps_its_new_role_access(
    tmp_path: Path,
) -> None:
    """Nothing serializes an add against a remove of the same id. The
    purge runs right after the store remove, so a re-add landing during
    the rest of the teardown keeps the role access it was just given
    instead of coming back denied to every role."""
    gated = GatedConnectionStore(tmp_path)
    service, config_store, policy_engine = make_file_service(
        tmp_path, connection_store=gated,
    )
    await service.add_upstream(DEFAULT_ORG_ID, make_github())
    await service.grant_role_access(DEFAULT_ORG_ID, "github")

    remove = asyncio.create_task(service.remove_upstream(DEFAULT_ORG_ID, "github"))
    await gated.entered.wait()
    await service.add_upstream(DEFAULT_ORG_ID, make_github())
    await service.grant_role_access(DEFAULT_ORG_ID, "github")
    gated.release.set()
    await remove

    for config in (await config_store.load(DEFAULT_ORG_ID), policy_engine.config):
        assert config.roles["admin"].settings.mcp_access.mcps.get("github") is True
        assert config.roles["admin"].settings.tool_access["github"] == FRESH_TOOL_ACCESS
