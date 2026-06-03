"""Storage factory — picks file or Mongo repositories based on mode.

Every ``Settings.mode`` path must produce the same ``StorageBundle``
shape. Domain services and controllers only see the ``*Repository``
protocol types; they do not know whether the underlying implementation
is a JSON file on disk or a Mongo collection.

Adding a new persistent store to MCP Hero means:
1. Defining the protocol in ``domain/ports/``.
2. Adding a file implementation.
3. Adding a Mongo implementation.
4. Declaring both on ``StorageBundle`` and constructing them in
   ``build_file_storage`` / ``build_cloud_storage`` below.

The factory is the only place in the app that knows both modes exist,
keeping the if/else out of hot paths.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

from mcpolis.adapters.distributed_lock_mongo import MongoDistributedLock
from mcpolis.adapters.distributed_lock_noop import NoOpDistributedLock
from mcpolis.adapters.event_stream_inprocess import InProcessEventStream
from mcpolis.adapters.event_stream_redis import RedisEventStream
from mcpolis.adapters.rate_limiter_inprocess import InProcessRateLimiter
from mcpolis.adapters.rate_limiter_redis import RedisRateLimiter
from mcpolis.adapters.session_revocation_inprocess import (
    InProcessSessionRevocationStore,
)
from mcpolis.adapters.session_revocation_redis import (
    RedisSessionRevocationStore,
)
from mcpolis.adapters.repositories.audit_repository import (
    AuditRepository as LegacyAuditRepository,
)
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.repositories.file_oauth_state_repository import (
    FileOAuthStateRepository,
)
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.adapters.repositories.file_tool_catalog_store import (
    FileToolCatalogStore,
)
from mcpolis.adapters.repositories.file_sandbox_file_repository import (
    FileSandboxFileRepository,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.repositories.file_upstream_config_store import (
    FileUpstreamConfigStore,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.adapters.repositories.mongo_client import (
    COLL_AUDIT,
    COLL_CONFIG,
    COLL_CONNECTIONS,
    COLL_OAUTH_STATE,
    COLL_SANDBOX_FILES,
    COLL_SANDBOX_REFS,
    COLL_TEMPLATE_VARS,
    COLL_TOOL_CATALOG,
    COLL_UPSTREAMS,
    MongoConnection,
    OrgScopedCollection,
    create_indexes,
)
from mcpolis.adapters.repositories.mongo_audit_repository import MongoAuditRepository
from mcpolis.adapters.repositories.mongo_config_repository import MongoConfigRepository
from mcpolis.adapters.repositories.mongo_connection_repository import (
    MongoConnectionRepository,
)
from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.repositories.mongo_sandbox_persistence_repository import (
    MongoSandboxPersistenceRepository,
)
from mcpolis.adapters.repositories.mongo_sandbox_file_repository import (
    MongoSandboxFileRepository,
)
from mcpolis.adapters.repositories.mongo_template_var_repository import (
    MongoTemplateVarRepository,
)
from mcpolis.adapters.repositories.mongo_oauth_state_repository import (
    MongoOAuthStateRepository,
)
from mcpolis.adapters.repositories.mongo_organization_repository import (
    MongoOrganizationRepository,
)
from mcpolis.adapters.repositories.mongo_tool_catalog_repository import (
    MongoToolCatalogRepository,
)
from mcpolis.adapters.repositories.mongo_upstream_config_repository import (
    MongoUpstreamConfigRepository,
)
from mcpolis.domain.model.settings import OAuthAppsConfig
from mcpolis.domain.ports import (
    ConfigRepository,
    OAuthStateRepository,
    OrganizationRepository,
    ToolCatalogRepository,
)
from mcpolis.domain.ports.distributed_lock import DistributedLock
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.rate_limiter import RateLimiter
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistenceRepository,
)
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.ports.session_revocation import SessionRevocationStore
from mcpolis.entrypoints.config import Settings

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class StorageBundle:
    """All storage + infra adapters wired up for the current mode.

    Existing call sites type their parameters against the legacy
    abstract bases in ``adapters/repositories`` (``ConnectionStore``,
    ``AuditRepository``, ``UpstreamConfigStore``). Both the file impls
    and the Mongo impls inherit from those, so the bundle uses the
    legacy types as its public shape. Pure-protocol fields
    (``config_repo``, ``oauth_state_repo``, ``organization_repo``,
    ``event_stream``, ``rate_limiter``) use the port protocols
    directly.

    The name is a minor misnomer now that ``event_stream`` and
    ``rate_limiter`` live alongside the repositories, but this stays a
    single bundle because ``create_app`` already threads it everywhere
    — splitting infra off into a sibling bundle would just double the
    plumbing for no win.
    """

    config_repo: ConfigRepository
    upstream_config_repo: UpstreamConfigStore
    connection_repo: ConnectionStore
    audit_repo: LegacyAuditRepository
    oauth_state_repo: OAuthStateRepository
    organization_repo: OrganizationRepository
    tool_catalog_repo: ToolCatalogRepository
    sandbox_persistence_repo: SandboxPersistenceRepository
    template_var_repo: TemplateVarRepository
    sandbox_file_repo: SandboxFileRepository
    event_stream: EventStream
    rate_limiter: RateLimiter
    distributed_lock: DistributedLock
    session_revocation: SessionRevocationStore
    # Mongo connection handle, owned by ``create_app`` in cloud mode so
    # it can be closed at shutdown. ``None`` in standalone mode.
    mongo: MongoConnection | None = None
    # Concrete file store handles — still needed by a few call sites
    # that haven't been ported to the protocol yet. None in cloud mode.
    file_config_store: FileConfigStore | None = None
    file_mcp_store: McpJsonStore | None = None


def build_storage(
    settings: Settings,
    oauth_apps: OAuthAppsConfig,
) -> StorageBundle:
    """Top-level entry point — inspects ``settings.mode``.

    Owns creation of the ``EventStream`` and ``RateLimiter`` so the
    caller doesn't need to pick the standalone/cloud impls. Standalone
    mode always gets in-process adapters; cloud mode gets Redis-backed
    adapters pointed at ``settings.redis_url``.
    """
    if settings.mode == "cloud":
        return build_cloud_storage(settings, oauth_apps)
    return build_file_storage(settings, oauth_apps)


def build_file_storage(
    settings: Settings,
    oauth_apps: OAuthAppsConfig,
) -> StorageBundle:
    """Standalone mode — JSON files on disk, in-process infra adapters."""
    event_stream: EventStream = InProcessEventStream()
    rate_limiter: RateLimiter = InProcessRateLimiter()
    distributed_lock: DistributedLock = NoOpDistributedLock()
    session_revocation: SessionRevocationStore = InProcessSessionRevocationStore()
    config_store = FileConfigStore(settings.config_path)
    mcp_store = McpJsonStore(settings.mcp_json_path)
    upstream_store = FileUpstreamConfigStore(
        mcp_store, config_store, oauth_apps=oauth_apps,
    )
    connection_store = FileConnectionStore(settings.data_dir)
    template_var_repo: TemplateVarRepository = FileTemplateVarRepository(settings.data_dir)
    sandbox_file_repo: SandboxFileRepository = FileSandboxFileRepository(settings.data_dir)
    audit_repo = FileAuditRepository(
        settings.audit_log_path,
        event_bus=event_stream,
        retention_days=settings.audit_retention_days,
    )
    oauth_state_repo = FileOAuthStateRepository(settings.data_dir)
    organization_repo = FileOrganizationRepository(settings.data_dir)
    tool_catalog_repo = FileToolCatalogStore(settings.data_dir)
    # Standalone mode keeps sandbox refs in memory — refs don't
    # survive a process restart. Documented standalone tradeoff
    # (see plan §"Resilience"); cloud mode flips to Mongo below.
    sandbox_persistence_repo: SandboxPersistenceRepository = (
        InMemorySandboxPersistenceRepository()
    )

    return StorageBundle(
        config_repo=config_store,
        upstream_config_repo=upstream_store,
        connection_repo=connection_store,
        audit_repo=audit_repo,
        oauth_state_repo=oauth_state_repo,
        organization_repo=organization_repo,
        tool_catalog_repo=tool_catalog_repo,
        sandbox_persistence_repo=sandbox_persistence_repo,
        template_var_repo=template_var_repo,
        sandbox_file_repo=sandbox_file_repo,
        event_stream=event_stream,
        rate_limiter=rate_limiter,
        distributed_lock=distributed_lock,
        session_revocation=session_revocation,
        mongo=None,
        file_config_store=config_store,
        file_mcp_store=mcp_store,
    )


def build_cloud_storage(
    settings: Settings,
    oauth_apps: OAuthAppsConfig,
) -> StorageBundle:
    """Cloud mode — MongoDB + Redis-backed infra adapters."""
    if not settings.encryption_key:
        raise RuntimeError(
            "MCPOLIS_ENCRYPTION_KEY is required in cloud mode "
            "(validate_startup_secrets should have caught this earlier)"
        )
    if not settings.mongo_uri:
        raise RuntimeError("MCPOLIS_MONGO_URI is required in cloud mode")
    if not settings.redis_url:
        raise RuntimeError("MCPOLIS_REDIS_URL is required in cloud mode")

    logger.info(
        "storage.mongo.connecting",
        mongo_uri=_redact_uri(settings.mongo_uri),
        mongo_db_name=settings.mongo_db_name,
    )
    mongo = MongoConnection(settings.mongo_uri, settings.mongo_db_name)
    db = mongo.database

    encryptor = FieldEncryptor.from_master_secret(settings.encryption_key)

    def scoped(name: str) -> OrgScopedCollection:
        return OrgScopedCollection(db[name], name, encryptor=encryptor)

    distributed_lock: DistributedLock = MongoDistributedLock(db)

    logger.info(
        "storage.redis.connecting",
        redis_url=_redact_uri(settings.redis_url),
    )
    event_stream: EventStream = RedisEventStream(settings.redis_url)
    rate_limiter: RateLimiter = RedisRateLimiter(settings.redis_url)
    session_revocation: SessionRevocationStore = RedisSessionRevocationStore(
        settings.redis_url,
    )

    organization_repo = MongoOrganizationRepository(db)
    config_repo = MongoConfigRepository(scoped(COLL_CONFIG))
    upstream_config_repo = MongoUpstreamConfigRepository(
        scoped(COLL_UPSTREAMS), scoped(COLL_CONFIG),
        oauth_apps=oauth_apps,
    )
    connection_repo = MongoConnectionRepository(scoped(COLL_CONNECTIONS))
    audit_repo = MongoAuditRepository(
        scoped(COLL_AUDIT), event_bus=event_stream,
    )
    oauth_state_repo = MongoOAuthStateRepository(
        scoped(COLL_OAUTH_STATE), encryptor,
    )
    tool_catalog_repo = MongoToolCatalogRepository(scoped(COLL_TOOL_CATALOG))
    # Cloud-mode sandbox refs go through Mongo so the in-memory
    # SandboxStateRegistry can be reconstructed across crashes and
    # the per-backend reconciler has a durable source of truth to
    # cross-reference against provider listings.
    sandbox_persistence_repo: SandboxPersistenceRepository = (
        MongoSandboxPersistenceRepository(scoped(COLL_SANDBOX_REFS))
    )
    template_var_repo: TemplateVarRepository = MongoTemplateVarRepository(scoped(COLL_TEMPLATE_VARS))
    sandbox_file_repo: SandboxFileRepository = MongoSandboxFileRepository(
        scoped(COLL_SANDBOX_FILES),
    )

    return StorageBundle(
        config_repo=config_repo,
        upstream_config_repo=upstream_config_repo,
        connection_repo=connection_repo,
        audit_repo=audit_repo,
        oauth_state_repo=oauth_state_repo,
        organization_repo=organization_repo,
        tool_catalog_repo=tool_catalog_repo,
        sandbox_persistence_repo=sandbox_persistence_repo,
        template_var_repo=template_var_repo,
        sandbox_file_repo=sandbox_file_repo,
        event_stream=event_stream,
        rate_limiter=rate_limiter,
        distributed_lock=distributed_lock,
        session_revocation=session_revocation,
        mongo=mongo,
        file_config_store=None,
        file_mcp_store=None,
    )


async def initialize_storage(
    bundle: StorageBundle,
    *,
    audit_retention_days: int = 7,
) -> None:
    """Async-side initialization that needs an event loop.

    - Cloud mode: create Mongo indexes, then sync membership rows for
      every org that already has users in its config doc (so
      ``list_user_orgs`` works for users added via the Team page).
      No default org is created — users create their own on signup.
    - Standalone mode: ensure the single ``default`` org exists (no-op
      — the file config store already seeded defaults synchronously
      during ``create_app``).
    """
    if bundle.mongo is not None:
        await create_indexes(
            bundle.mongo.database,
            audit_retention_days=audit_retention_days,
        )
        # Sync memberships for all existing orgs. Users added via the
        # Team page only write to ``config.users``; the membership row
        # is created here (idempotent upsert) so ``list_user_orgs``
        # and the Team page's "Joined/Pending" status work correctly.
        all_orgs = await bundle.organization_repo.list_organizations()
        for org in all_orgs:
            config = await bundle.config_repo.load(org.id)
            for email, user_def in config.users.items():
                await bundle.organization_repo.add_membership(
                    org.id, email, user_def.role,
                )
    else:
        # Standalone mode: ensure the default org + default roles.
        await bundle.organization_repo.ensure_default_org()
        await bundle.config_repo.ensure_defaults("default")


def _redact_uri(uri: str) -> str:
    """Hide credentials in Mongo URIs before logging."""
    if "@" not in uri:
        return uri
    scheme, _, rest = uri.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***:***@{host}"


