"""Pin that user-supplied upstream config is encrypted at rest.

The ``upstreams`` collection holds command/args/env/headers/url and
OAuth client_id/client_secret/scopes for every upstream MCP. Those are
the most credential-dense values in the system, so the security posture
hinges on them landing in Mongo as ciphertext rather than plaintext.

These tests pair with the migration script under
``mcpolis.adapters.repositories.migrations.upstreams_encrypt_phase_a``
which back-fills existing plaintext rows; after migration, the assertions
below also describe the steady state for any newly-seeded test database.
"""
from __future__ import annotations

import json

import pytest

from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.migrations.upstreams_encrypt_phase_a import (
    run_migration,
)
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONFIG,
    COLL_UPSTREAMS,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_upstream_config_repository import (
    MongoUpstreamConfigRepository,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID

from tests.unit.mongo_fixture import (
    MONGO_URI,
    mongo_available,
    temp_mongo_database,
)

ENCRYPTION_KEY = "test-master-secret-32-bytes-long!"
PLAINTEXT_CLIENT_SECRET = "super-secret-client-secret-do-not-leak"
PLAINTEXT_ENV_VALUE = "GH_TOKEN_plaintext_must_not_appear_in_db"
PLAINTEXT_BEARER = "Bearer leaky-bearer-must-not-appear"


def _make_encryptor() -> FieldEncryptor:
    return FieldEncryptor.from_master_secret(ENCRYPTION_KEY)


def _make_stdio_upstream(
    upstream_id: str = "github",
    *,
    client_secret: str = PLAINTEXT_CLIENT_SECRET,
    env_value: str = PLAINTEXT_ENV_VALUE,
) -> UpstreamDefinition:
    return UpstreamDefinition(
        id=upstream_id,
        display_name="GitHub",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": env_value},
        ),
        http=None,
        auth=UpstreamAuthConfig(
            mode=AuthMode.service_account,
            client_id="client-abc",
            client_secret=client_secret,
            scopes=["repo"],
        ),
    )


def _make_http_upstream(upstream_id: str = "notion") -> UpstreamDefinition:
    return UpstreamDefinition(
        id=upstream_id,
        display_name="Notion",
        transport=TransportType.streamable_http,
        stdio=None,
        http=HttpTransportConfig(
            url="https://mcp.notion.com",
            headers={"Authorization": PLAINTEXT_BEARER},
        ),
        auth=UpstreamAuthConfig(
            mode=AuthMode.per_user_oauth,
            client_id="notion-client",
            client_secret=PLAINTEXT_CLIENT_SECRET,
            scopes=["read"],
        ),
    )


def _make_repos(
    db: object, *, encryptor: FieldEncryptor | None,
) -> tuple[MongoUpstreamConfigRepository, OrgScopedCollection]:
    upstreams_scoped = OrgScopedCollection(
        db[COLL_UPSTREAMS], COLL_UPSTREAMS, encryptor=encryptor,  # type: ignore[index]
    )
    config_scoped = OrgScopedCollection(
        db[COLL_CONFIG], COLL_CONFIG, encryptor=encryptor,  # type: ignore[index]
    )
    repo = MongoUpstreamConfigRepository(upstreams_scoped, config_scoped)
    return repo, upstreams_scoped


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_upstream_round_trips_through_repo() -> None:
    """Write → read returns the same UpstreamDefinition, including
    secrets — encryption is invisible to the caller."""
    async with temp_mongo_database() as db:
        repo, _ = _make_repos(db, encryptor=_make_encryptor())
        upstream = _make_stdio_upstream()
        await repo.add(DEFAULT_ORG_ID, upstream)

        loaded = await repo.get(DEFAULT_ORG_ID, upstream.id)
        assert loaded is not None
        assert loaded.id == upstream.id
        assert loaded.stdio is not None
        assert loaded.stdio.command == "npx"
        assert loaded.stdio.env == {"GITHUB_TOKEN": PLAINTEXT_ENV_VALUE}
        assert loaded.auth.client_id == "client-abc"
        assert loaded.auth.client_secret == PLAINTEXT_CLIENT_SECRET
        assert loaded.auth.scopes == ["repo"]


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_stdio_secrets_are_ciphertext_at_rest() -> None:
    """The raw Mongo doc must hold ``enc:v1:...`` blobs and contain
    none of the plaintext credential strings — the security invariant."""
    async with temp_mongo_database() as db:
        repo, _ = _make_repos(db, encryptor=_make_encryptor())
        await repo.add(DEFAULT_ORG_ID, _make_stdio_upstream())

        # Bypass the wrapper to read the doc as it sits on disk.
        raw = await db[COLL_UPSTREAMS].find_one({"upstream_id": "github"})
        assert raw is not None
        sc_blob = raw["server_config_encrypted"]
        opts_blob = raw["options_encrypted"]
        assert isinstance(sc_blob, str) and sc_blob.startswith("enc:v1:")
        assert isinstance(opts_blob, str) and opts_blob.startswith("enc:v1:")

        # No plaintext secret appears anywhere in the raw document.
        full_doc_text = json.dumps(raw, default=str)
        assert PLAINTEXT_CLIENT_SECRET not in full_doc_text
        assert PLAINTEXT_ENV_VALUE not in full_doc_text

        # Legacy plaintext keys must not be written by the new path.
        assert "server_config" not in raw
        assert "options" not in raw


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_http_secrets_are_ciphertext_at_rest() -> None:
    """Same invariant for the http transport — bearer tokens embedded
    in headers, OAuth client_secret in options."""
    async with temp_mongo_database() as db:
        repo, _ = _make_repos(db, encryptor=_make_encryptor())
        await repo.add(DEFAULT_ORG_ID, _make_http_upstream())

        raw = await db[COLL_UPSTREAMS].find_one({"upstream_id": "notion"})
        assert raw is not None
        full_doc_text = json.dumps(raw, default=str)
        assert PLAINTEXT_BEARER not in full_doc_text
        assert PLAINTEXT_CLIENT_SECRET not in full_doc_text


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_update_preserves_options_when_only_server_config_changes() -> None:
    """``update_server_config`` should round-trip the existing
    encrypted options blob without losing fields."""
    async with temp_mongo_database() as db:
        repo, _ = _make_repos(db, encryptor=_make_encryptor())
        await repo.add(DEFAULT_ORG_ID, _make_stdio_upstream())

        await repo.update_server_config(
            DEFAULT_ORG_ID,
            "github",
            {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "rotated-value"},
            },
        )

        loaded = await repo.get(DEFAULT_ORG_ID, "github")
        assert loaded is not None
        assert loaded.stdio is not None
        assert loaded.stdio.env == {"GITHUB_TOKEN": "rotated-value"}
        # Options preserved.
        assert loaded.auth.client_id == "client-abc"
        assert loaded.auth.client_secret == PLAINTEXT_CLIENT_SECRET


# ---------------------------------------------------------------------------
# Migration: existing plaintext rows + stale config.upstreams
# ---------------------------------------------------------------------------


async def _seed_plaintext_upstream_doc(
    db: object, upstream_id: str = "legacy-github",
) -> None:
    # Insert a doc in the pre-migration shape, bypassing the repo so
    # the encryption layer doesn't run.
    await db[COLL_UPSTREAMS].insert_one(  # type: ignore[index]
        {
            "org_id": DEFAULT_ORG_ID,
            "upstream_id": upstream_id,
            "server_config": {
                "command": "npx",
                "args": ["-y", "legacy-server"],
                "env": {"TOKEN": PLAINTEXT_ENV_VALUE},
            },
            "options": {
                "display_name": "Legacy",
                "auth_mode": "per_user_oauth",
                "client_id": "legacy-client",
                "client_secret": PLAINTEXT_CLIENT_SECRET,
                "scopes": ["repo"],
                "default_arguments": {},
            },
        },
    )


async def _seed_stale_config_upstreams(db: object) -> None:
    await db[COLL_CONFIG].insert_one(  # type: ignore[index]
        {
            "org_id": DEFAULT_ORG_ID,
            "config": {
                "roles": {},
                "users": {},
                "upstreams": {
                    "legacy-github": {
                        "display_name": "Legacy",
                        "auth_mode": "per_user_oauth",
                        "client_id": "legacy-client",
                        "client_secret": PLAINTEXT_CLIENT_SECRET,
                        "scopes": ["repo"],
                        "default_arguments": {},
                    },
                },
            },
        },
    )


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_migration_dry_run_makes_no_writes() -> None:
    async with temp_mongo_database() as db:
        await _seed_plaintext_upstream_doc(db)
        await _seed_stale_config_upstreams(db)

        upstreams_summary, config_summary = await run_migration(
            mongo_uri=_mongo_uri_for(db),
            mongo_db=db.name,
            encryption_key=ENCRYPTION_KEY,
            dry_run=True,
        )
        assert upstreams_summary.encrypted == 1
        assert config_summary.cleaned == 1

        # Dry-run wrote nothing.
        raw = await db[COLL_UPSTREAMS].find_one({"upstream_id": "legacy-github"})
        assert raw is not None
        assert "server_config" in raw
        assert "options" in raw
        assert "server_config_encrypted" not in raw

        cfg = await db[COLL_CONFIG].find_one({"org_id": DEFAULT_ORG_ID})
        assert cfg is not None
        assert "upstreams" in cfg["config"]


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_migration_real_run_encrypts_and_strips_stale_config() -> None:
    async with temp_mongo_database() as db:
        await _seed_plaintext_upstream_doc(db)
        await _seed_stale_config_upstreams(db)

        upstreams_summary, config_summary = await run_migration(
            mongo_uri=_mongo_uri_for(db),
            mongo_db=db.name,
            encryption_key=ENCRYPTION_KEY,
            dry_run=False,
        )
        assert upstreams_summary.encrypted == 1
        assert config_summary.cleaned == 1

        raw = await db[COLL_UPSTREAMS].find_one({"upstream_id": "legacy-github"})
        assert raw is not None
        assert raw["server_config_encrypted"].startswith("enc:v1:")
        assert raw["options_encrypted"].startswith("enc:v1:")
        assert "server_config" not in raw
        assert "options" not in raw
        assert PLAINTEXT_CLIENT_SECRET not in json.dumps(raw, default=str)
        assert PLAINTEXT_ENV_VALUE not in json.dumps(raw, default=str)

        # The repo should now load the migrated row with full fidelity.
        repo, _ = _make_repos(db, encryptor=_make_encryptor())
        loaded = await repo.get(DEFAULT_ORG_ID, "legacy-github")
        assert loaded is not None
        assert loaded.auth.client_secret == PLAINTEXT_CLIENT_SECRET
        assert loaded.stdio is not None
        assert loaded.stdio.env == {"TOKEN": PLAINTEXT_ENV_VALUE}

        # Stale ``config.upstreams`` is gone; the rest of the config
        # doc survives.
        cfg = await db[COLL_CONFIG].find_one({"org_id": DEFAULT_ORG_ID})
        assert cfg is not None
        assert "upstreams" not in cfg["config"]
        assert "roles" in cfg["config"]
        assert "users" in cfg["config"]


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_migration_is_idempotent() -> None:
    async with temp_mongo_database() as db:
        await _seed_plaintext_upstream_doc(db)
        await _seed_stale_config_upstreams(db)

        # First run: real.
        await run_migration(
            mongo_uri=_mongo_uri_for(db),
            mongo_db=db.name,
            encryption_key=ENCRYPTION_KEY,
            dry_run=False,
        )
        # Second run: must be a no-op on counts.
        upstreams_summary, config_summary = await run_migration(
            mongo_uri=_mongo_uri_for(db),
            mongo_db=db.name,
            encryption_key=ENCRYPTION_KEY,
            dry_run=False,
        )
        assert upstreams_summary.encrypted == 0
        assert upstreams_summary.already_encrypted == 1
        assert config_summary.cleaned == 0


def _mongo_uri_for(_db: object) -> str:
    """Resolve the URI the test fixture itself uses, so the migration's
    own ``MongoConnection`` reaches the same ephemeral test database.
    The skipif guard ensures ``MONGO_URI`` is set whenever this runs."""
    assert MONGO_URI is not None
    return MONGO_URI
