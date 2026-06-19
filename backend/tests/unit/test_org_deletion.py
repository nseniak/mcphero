"""Organization-deletion cascade tests.

Two layers:

1. **End-to-end purge via BOTH delete paths** (Mongo-only). Production
   org deletion only happens in cloud mode — standalone rejects it at
   the repo level (``FileOrganizationRepository.delete_organization``
   raises) — so these run against a throwaway Mongo database. They seed
   an org with the full spread of state, delete it through the dashboard
   path (``OrgService.delete_organization``) and the superadmin path
   (the ``delete_organization`` MCP tool), and assert zero org-scoped
   rows remain in every collection. An equivalence test pins that the
   two paths leave identical residue and never touch a bystander org.

2. **Per-repo ``delete_all_for_org`` on the file / in-memory backends.**
   The Mongo side of every repo is exercised by the end-to-end test; these
   cover the standalone backends for parity (and prove cross-org scoping
   on the backends whose on-disk layout is multi-org-capable).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.connection_store import ConnectionStore, OAuthToken
from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_sandbox_file_repository import (
    FileSandboxFileRepository,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.repositories.file_tool_catalog_store import FileToolCatalogStore
from mcpolis.adapters.repositories.file_upstream_config_store import (
    FileUpstreamConfigStore,
)
from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.repositories.mongo_audit_repository import MongoAuditRepository
from mcpolis.adapters.repositories.mongo_client import (
    COLL_AUDIT,
    COLL_CONFIG,
    COLL_CONNECTIONS,
    COLL_SANDBOX_FILES,
    COLL_SANDBOX_REFS,
    COLL_SERVICE_TOKENS,
    COLL_TEMPLATE_VARS,
    COLL_TOOL_CATALOG,
    COLL_UPSTREAMS,
    MotorDatabase,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_config_repository import MongoConfigRepository
from mcpolis.adapters.repositories.mongo_connection_repository import (
    MongoConnectionRepository,
)
from mcpolis.adapters.repositories.mongo_organization_repository import (
    MongoOrganizationRepository,
)
from mcpolis.adapters.repositories.mongo_sandbox_file_repository import (
    MongoSandboxFileRepository,
)
from mcpolis.adapters.repositories.mongo_sandbox_persistence_repository import (
    MongoSandboxPersistenceRepository,
)
from mcpolis.adapters.repositories.mongo_service_token_repository import (
    MongoServiceTokenRepository,
)
from mcpolis.adapters.repositories.mongo_template_var_repository import (
    MongoTemplateVarRepository,
)
from mcpolis.adapters.repositories.mongo_tool_catalog_repository import (
    MongoToolCatalogRepository,
)
from mcpolis.adapters.repositories.mongo_upstream_config_repository import (
    MongoUpstreamConfigRepository,
)
from mcpolis.domain.ports import Organization
from mcpolis.domain.ports.sandbox_persistence_repository import SandboxPersistedRef
from mcpolis.domain.ports.tool_catalog_repository import ToolCatalogSnapshot
from mcpolis.domain.services.org_service import OrgService
from mcpolis.entrypoints.controllers.superadmin_controller import (
    create_superadmin_mcp_server,
)
from tests.unit.factories import (
    make_audit_entry,
    make_discovered_tool,
    make_full_access_config,
    make_service_token_record,
    make_upstream_definition,
)
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


# ---------------------------------------------------------------------------
# Shared builders / seeders
# ---------------------------------------------------------------------------

def _token() -> OAuthToken:
    return OAuthToken(
        access_token="access-123",
        refresh_token="refresh-456",
        expires_at=datetime(2026, 6, 1, tzinfo=UTC),
        scopes=["read", "write"],
    )


def _make_repos(db: MotorDatabase) -> SimpleNamespace:
    """Wire every Mongo repo + a fully-injected ``OrgService`` against one
    throwaway database. Mirrors ``build_cloud_storage`` but local to the
    test so the suite stays decoupled from ``create_app``."""
    encryptor = FieldEncryptor.from_master_secret("unit-test-key")

    def scoped(name: str) -> OrgScopedCollection:
        return OrgScopedCollection(db[name], name, encryptor=encryptor)

    org_repo = MongoOrganizationRepository(db)
    config_repo = MongoConfigRepository(scoped(COLL_CONFIG))
    connection_repo = MongoConnectionRepository(scoped(COLL_CONNECTIONS))
    upstream_config_repo = MongoUpstreamConfigRepository(
        scoped(COLL_UPSTREAMS), scoped(COLL_CONFIG),
    )
    tool_catalog_repo = MongoToolCatalogRepository(scoped(COLL_TOOL_CATALOG))
    sandbox_persistence_repo = MongoSandboxPersistenceRepository(
        scoped(COLL_SANDBOX_REFS),
    )
    template_var_repo = MongoTemplateVarRepository(scoped(COLL_TEMPLATE_VARS))
    sandbox_file_repo = MongoSandboxFileRepository(scoped(COLL_SANDBOX_FILES))
    service_token_repo = MongoServiceTokenRepository(db[COLL_SERVICE_TOKENS])
    audit_repo = MongoAuditRepository(scoped(COLL_AUDIT))

    org_service = OrgService(
        org_repo=org_repo,
        config_repo=config_repo,
        service_token_repo=service_token_repo,
        connection_repo=connection_repo,
        upstream_config_repo=upstream_config_repo,
        tool_catalog_repo=tool_catalog_repo,
        sandbox_persistence_repo=sandbox_persistence_repo,
        template_var_repo=template_var_repo,
        sandbox_file_repo=sandbox_file_repo,
        audit_repo=audit_repo,
    )
    return SimpleNamespace(
        org_repo=org_repo,
        config_repo=config_repo,
        connection_repo=connection_repo,
        upstream_config_repo=upstream_config_repo,
        tool_catalog_repo=tool_catalog_repo,
        sandbox_persistence_repo=sandbox_persistence_repo,
        template_var_repo=template_var_repo,
        sandbox_file_repo=sandbox_file_repo,
        service_token_repo=service_token_repo,
        audit_repo=audit_repo,
        org_service=org_service,
    )


async def _seed_connection(
    conn: ConnectionStore, org_id: str, upstream: str, user: str,
) -> None:
    await conn.put_admin_token(
        org_id, upstream, _token(), authorized_by="admin@co.com",
    )
    await conn.put_user_token(org_id, user, upstream, _token())
    await conn.put_client_info(org_id, upstream, user, {"client_id": "cid-1"})
    await conn.put_oauth_metadata(org_id, upstream, user, {"issuer": "iss-1"})
    await conn.put_pending_code(org_id, upstream, user, "code", "state")
    await conn.record_refresh_failure(org_id, upstream, user)
    await conn.mark_notified(org_id, upstream, user)
    await conn.set_disabled(org_id, upstream)
    await conn.set_connection_error(org_id, upstream, "boom")
    await conn.set_started_config_hash(org_id, upstream, "hash-1")


async def _seed_org(
    repos: SimpleNamespace, slug: str, display_name: str, email: str,
) -> Organization:
    """Create an org and write the full spread of org-scoped state."""
    upstream = "github"
    org = await repos.org_repo.create_organization(
        slug=slug, display_name=display_name, created_by_email=email,
    )
    await repos.org_repo.add_membership(org.id, email, "default")
    await repos.config_repo.save(
        org.id, make_full_access_config([upstream], [email]),
    )
    await _seed_connection(repos.connection_repo, org.id, upstream, email)
    await repos.upstream_config_repo.add(
        org.id, make_upstream_definition(id=upstream),
    )
    await repos.tool_catalog_repo.upsert_upstream(
        org.id, upstream,
        ToolCatalogSnapshot(tools=[make_discovered_tool(upstream)]),
    )
    await repos.sandbox_persistence_repo.upsert(
        SandboxPersistedRef(
            provider="e2b",
            org_id=org.id,
            upstream_id=upstream,
            mcpolis_instance="inst-1",
            sandbox_id="sb-1",
            paused_snapshot_id=None,
            pid=None,
            metadata={},
            cached_server_info=None,
            cached_self_description=None,
            last_updated=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    await repos.template_var_repo.set(
        org.id, upstream, "API_KEY", "secret-value", is_secret=True,
    )
    await repos.sandbox_file_repo.set(
        org.id, upstream, "cred.json", "{}", target_path="/cred.json",
    )
    await repos.service_token_repo.create(
        make_service_token_record(org_id=org.id, raw_token=f"svct_{slug}"),
    )
    await repos.audit_repo.log(org.id, make_audit_entry(org_id=org.id))
    return org


async def _assert_org_purged(
    repos: SimpleNamespace, org: Organization, slug: str, email: str,
) -> None:
    """Assert ZERO org-scoped rows remain for ``org`` in every collection."""
    upstream = "github"
    org_id = org.id

    # org doc + memberships
    assert await repos.org_repo.get_by_slug(slug) is None
    assert await repos.org_repo.list_memberships(org_id) == []

    # config doc (users + roles)
    config = await repos.config_repo.load(org_id)
    assert not config.users
    assert not config.roles

    # connection store — every key shape
    assert await repos.connection_repo.get_admin_token(org_id, upstream) is None
    assert await repos.connection_repo.get_user_token(org_id, email, upstream) is None
    assert await repos.connection_repo.get_client_info(org_id, upstream, email) is None
    assert await repos.connection_repo.get_oauth_metadata(
        org_id, upstream, email,
    ) is None
    assert await repos.connection_repo.get_refresh_failures(
        org_id, upstream, email,
    ) is None
    assert await repos.connection_repo.was_notified(org_id, upstream, email) is False
    assert await repos.connection_repo.is_enabled(org_id, upstream) is True
    assert await repos.connection_repo.get_connection_error(org_id, upstream) is None
    assert await repos.connection_repo.get_started_config_hash(
        org_id, upstream,
    ) is None
    assert await repos.connection_repo.get_all_stored_tokens(org_id) == []

    # upstreams / tool catalog / sandbox refs / template vars / sandbox files
    assert await repos.upstream_config_repo.get_all(org_id) == []
    assert await repos.tool_catalog_repo.load_all(org_id) == {}
    assert await repos.sandbox_persistence_repo.list_for_org(org_id=org_id) == []
    assert await repos.template_var_repo.list_summaries(org_id, upstream) == []
    assert await repos.sandbox_file_repo.list_summaries(org_id, upstream) == []

    # service tokens + audit
    assert await repos.service_token_repo.list_for_org(org_id) == []
    assert await repos.audit_repo.search(org_id) == []


# ---------------------------------------------------------------------------
# End-to-end: full purge via both delete paths (Mongo-only)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_dashboard_delete_purges_every_collection() -> None:
    """The dashboard path (``OrgService.delete_organization``) must leave
    zero org-scoped rows behind in any collection."""
    async with temp_mongo_database() as db:
        repos = _make_repos(db)
        org = await _seed_org(repos, "acme", "Acme", "admin@acme.com")

        await repos.org_service.delete_organization(org.id)

        await _assert_org_purged(repos, org, "acme", "admin@acme.com")


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_superadmin_delete_purges_every_collection() -> None:
    """The superadmin MCP tool routes through the same service method, so
    it must purge exactly what the dashboard path does."""
    async with temp_mongo_database() as db:
        repos = _make_repos(db)
        org = await _seed_org(repos, "globex", "Globex", "admin@globex.com")

        # ``runtime_manager`` is only consulted for the dry-run preview;
        # ``get_cached`` returning None means upstream_count=0.
        runtime_manager = SimpleNamespace(get_cached=lambda _org_id: None)
        server = create_superadmin_mcp_server(
            org_repo=repos.org_repo,
            runtime_manager=runtime_manager,  # type: ignore[arg-type]
            org_service=repos.org_service,
        )
        await server.call_tool(
            "delete_organization", {"slug": "globex", "confirm": True},
        )

        await _assert_org_purged(repos, org, "globex", "admin@globex.com")


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_both_delete_paths_are_equivalent_and_org_scoped() -> None:
    """Dashboard and superadmin deletions leave identical (empty) residue,
    and neither touches a bystander org that is never deleted."""
    async with temp_mongo_database() as db:
        repos = _make_repos(db)
        org_dash = await _seed_org(repos, "dash", "Dash", "a@dash.com")
        org_super = await _seed_org(repos, "super", "Super", "b@super.com")
        bystander = await _seed_org(repos, "keep", "Keep", "c@keep.com")

        # Dashboard path.
        await repos.org_service.delete_organization(org_dash.id)
        # Superadmin path.
        runtime_manager = SimpleNamespace(get_cached=lambda _org_id: None)
        server = create_superadmin_mcp_server(
            org_repo=repos.org_repo,
            runtime_manager=runtime_manager,  # type: ignore[arg-type]
            org_service=repos.org_service,
        )
        await server.call_tool(
            "delete_organization", {"slug": "super", "confirm": True},
        )

        await _assert_org_purged(repos, org_dash, "dash", "a@dash.com")
        await _assert_org_purged(repos, org_super, "super", "b@super.com")

        # Bystander is fully intact across both deletions.
        assert await repos.org_repo.get_by_slug("keep") is not None
        assert (await repos.config_repo.load(bystander.id)).users
        assert await repos.connection_repo.get_admin_token(
            bystander.id, "github",
        ) is not None
        assert await repos.upstream_config_repo.get_all(bystander.id) != []
        assert await repos.tool_catalog_repo.load_all(bystander.id) != {}
        assert await repos.sandbox_persistence_repo.list_for_org(
            org_id=bystander.id,
        ) != []
        assert await repos.template_var_repo.list_summaries(
            bystander.id, "github",
        ) != []
        assert await repos.sandbox_file_repo.list_summaries(
            bystander.id, "github",
        ) != []
        assert await repos.service_token_repo.list_for_org(bystander.id) != []
        assert await repos.audit_repo.search(bystander.id) != []


# ---------------------------------------------------------------------------
# Per-repo delete_all_for_org on the file / in-memory backends
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_config_delete_for_org(tmp_path: Path) -> None:
    store = FileConfigStore(tmp_path / "config.json")
    await store.save("default", make_full_access_config(["github"], ["a@co.com"]))
    assert (await store.load("default")).users

    await store.delete_for_org("default")

    config = await store.load("default")
    assert not config.users
    assert not config.roles
    # Idempotent on a now-empty store.
    await store.delete_for_org("default")


@pytest.mark.asyncio
async def test_file_upstream_config_delete_all_for_org(tmp_path: Path) -> None:
    mcp_store = McpJsonStore(tmp_path / "mcp.json")
    config_store = FileConfigStore(tmp_path / "config.json")
    store = FileUpstreamConfigStore(mcp_store, config_store)
    await store.add("default", make_upstream_definition(id="github"))
    await store.add("default", make_upstream_definition(id="slack"))
    assert len(await store.get_all("default")) == 2

    await store.delete_all_for_org("default")

    assert await store.get_all("default") == []
    await store.delete_all_for_org("default")  # idempotent


@pytest.mark.asyncio
async def test_file_tool_catalog_delete_all_for_org(tmp_path: Path) -> None:
    store = FileToolCatalogStore(tmp_path)
    snap = ToolCatalogSnapshot(tools=[make_discovered_tool("github")])
    await store.upsert_upstream("org-a", "github", snap)
    await store.upsert_upstream("org-a", "slack", snap)
    await store.upsert_upstream("org-b", "github", snap)

    await store.delete_all_for_org("org-a")

    assert await store.load_all("org-a") == {}
    # Per-org file layout: the other org survives.
    assert set(await store.load_all("org-b")) == {"github"}
    await store.delete_all_for_org("org-a")  # idempotent


@pytest.mark.asyncio
async def test_file_template_var_delete_all_for_org(tmp_path: Path) -> None:
    store = FileTemplateVarRepository(tmp_path)
    await store.set("org-a", "github", "K1", "v1", is_secret=True)
    await store.set("org-a", "slack", "K2", "v2", is_secret=False)
    await store.set("org-b", "github", "K3", "v3", is_secret=True)

    removed = await store.delete_all_for_org("org-a")

    assert removed == 2
    assert await store.list_summaries("org-a", "github") == []
    assert await store.list_summaries("org-a", "slack") == []
    # The other org's vars survive (file layout is keyed by org).
    assert len(await store.list_summaries("org-b", "github")) == 1
    assert await store.delete_all_for_org("org-a") == 0


@pytest.mark.asyncio
async def test_file_sandbox_file_delete_all_for_org(tmp_path: Path) -> None:
    store = FileSandboxFileRepository(tmp_path)
    await store.set("org-a", "github", "f1", "c1", target_path="/f1")
    await store.set("org-a", "slack", "f2", "c2", target_path="/f2")
    await store.set("org-b", "github", "f3", "c3", target_path="/f3")

    removed = await store.delete_all_for_org("org-a")

    assert removed == 2
    assert await store.list_summaries("org-a", "github") == []
    assert len(await store.list_summaries("org-b", "github")) == 1
    assert await store.delete_all_for_org("org-a") == 0


@pytest.mark.asyncio
async def test_inmemory_sandbox_persistence_delete_all_for_org() -> None:
    store = InMemorySandboxPersistenceRepository()

    def _ref(org_id: str, upstream_id: str) -> SandboxPersistedRef:
        return SandboxPersistedRef(
            provider="e2b",
            org_id=org_id,
            upstream_id=upstream_id,
            mcpolis_instance="inst-1",
            sandbox_id="sb",
            paused_snapshot_id=None,
            pid=None,
            metadata={},
            cached_server_info=None,
            cached_self_description=None,
            last_updated=datetime(2026, 1, 1, tzinfo=UTC),
        )

    await store.upsert(_ref("org-a", "github"))
    await store.upsert(_ref("org-a", "slack"))
    await store.upsert(_ref("org-b", "github"))

    removed = await store.delete_all_for_org(org_id="org-a")

    assert removed == 2
    assert await store.list_for_org(org_id="org-a") == []
    assert len(await store.list_for_org(org_id="org-b")) == 1
    assert await store.delete_all_for_org(org_id="org-a") == 0


@pytest.mark.asyncio
async def test_file_audit_delete_for_org(tmp_path: Path) -> None:
    repo: AuditRepository = FileAuditRepository(tmp_path / "audit.jsonl")
    await repo.log("org-a", make_audit_entry(org_id="org-a"))
    await repo.log("org-a", make_audit_entry(org_id="org-a", user_id="u2"))
    await repo.log("org-b", make_audit_entry(org_id="org-b"))

    removed = await repo.delete_for_org("org-a")

    assert removed == 2
    # ``search`` ignores org scoping on the file backend (single-org
    # deployment), so assert via the cross-org pass: only org-b survives.
    remaining = await repo.search_cross_org()
    assert len(remaining) == 1
    assert remaining[0]["org_id"] == "org-b"
    assert await repo.delete_for_org("org-a") == 0
