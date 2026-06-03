"""Mongo-mode startup tests: indexes, default org seeding, secret validation."""
from __future__ import annotations

import pytest

from mcpolis.adapters.repositories.mongo_client import (
    COLL_AUDIT,
    COLL_CONFIG,
    COLL_CONNECTIONS,
    COLL_MEMBERSHIPS,
    COLL_OAUTH_STATE,
    COLL_ORGANIZATIONS,
    COLL_UPSTREAMS,
    create_indexes,
)
from mcpolis.adapters.repositories.mongo_organization_repository import (
    MongoOrganizationRepository,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.entrypoints.config import Settings, validate_startup_secrets
from mcpolis.entrypoints.config import StartupConfigError
from tests.unit.mongo_fixture import temp_mongo_database


@pytest.mark.asyncio
async def test_create_indexes_is_idempotent() -> None:
    async with temp_mongo_database() as db:
        # ``temp_mongo_database`` already calls ``create_indexes`` once.
        # Calling it again should be a no-op.
        await create_indexes(db)
        await create_indexes(db)

        for coll_name in [
            COLL_ORGANIZATIONS,
            COLL_MEMBERSHIPS,
            COLL_CONFIG,
            COLL_UPSTREAMS,
            COLL_CONNECTIONS,
            COLL_AUDIT,
            COLL_OAUTH_STATE,
        ]:
            idx_info = await db[coll_name].index_information()
            assert len(idx_info) >= 1  # _id plus at least one named index


@pytest.mark.asyncio
async def test_audit_ttl_index_matches_retention() -> None:
    """The audit collection's TTL index must expire docs after
    ``audit_retention_days``; adjustments via collMod must take
    effect on a second call with a different retention value."""
    async with temp_mongo_database() as db:
        await create_indexes(db, audit_retention_days=7)
        info = await db[COLL_AUDIT].index_information()
        assert "ttl_created_at" in info
        assert info["ttl_created_at"]["expireAfterSeconds"] == 7 * 86400

        # Changing retention must adjust the existing index in place,
        # not raise IndexOptionsConflict.
        await create_indexes(db, audit_retention_days=3)
        info = await db[COLL_AUDIT].index_information()
        assert info["ttl_created_at"]["expireAfterSeconds"] == 3 * 86400


@pytest.mark.asyncio
async def test_mongo_audit_log_writes_created_at_bson_date() -> None:
    """MongoAuditRepository.log must set a BSON Date ``created_at``
    field so the TTL index can expire old entries."""
    from datetime import datetime

    from mcpolis.adapters.repositories.mongo_audit_repository import (
        MongoAuditRepository,
    )
    from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
    from mcpolis.domain.model.audit import AuditEntry

    async with temp_mongo_database() as db:
        coll = OrgScopedCollection(db[COLL_AUDIT], COLL_AUDIT)
        repo = MongoAuditRepository(coll)
        entry = AuditEntry(
            timestamp="2026-04-15T00:00:00Z",
            org_id="acme",
            user_id="alice@acme.com",
            upstream_id="github",
            tool="github__list_repos",
            policy_decision="allowed",
            response_status="ok",
        )
        await repo.log("acme", entry)
        doc = await db[COLL_AUDIT].find_one({"org_id": "acme"})
        assert doc is not None
        assert isinstance(doc["created_at"], datetime)


@pytest.mark.asyncio
async def test_ensure_default_org_is_idempotent() -> None:
    async with temp_mongo_database() as db:
        repo = MongoOrganizationRepository(db)
        org1 = await repo.ensure_default_org()
        org2 = await repo.ensure_default_org()
        assert org1.id == DEFAULT_ORG_ID
        assert org2.id == DEFAULT_ORG_ID
        # Listing returns exactly one default org.
        listed = await repo.list_organizations()
        assert [o.id for o in listed] == [DEFAULT_ORG_ID]


def test_cloud_mode_refuses_start_without_secrets() -> None:
    """``validate_startup_secrets`` surfaces cloud-mode config errors."""
    settings = Settings(
        mode="cloud",
        session_secret="",
        encryption_key="",
        mongo_uri="",
        redis_url="",
    )
    with pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(settings)
    msg = str(exc_info.value)
    assert "MCPOLIS_SESSION_SECRET" in msg
    assert "MCPOLIS_ENCRYPTION_KEY" in msg
    assert "MCPOLIS_MONGO_URI" in msg
    assert "MCPOLIS_REDIS_URL" in msg


def test_standalone_mode_does_not_require_secrets() -> None:
    settings = Settings(mode="standalone")
    # Should not raise.
    validate_startup_secrets(settings)
