"""Mongo-backed secret repo tests + the encryption-at-rest invariant.

Skipped automatically when ``MCPOLIS_TEST_MONGO_URI`` is unset / no
Mongo is reachable on localhost (see ``mongo_fixture.MONGO_URI``).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.mongo_client import (
    COLL_TEMPLATE_VARS,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_template_var_repository import (
    MongoTemplateVarRepository,
)

from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


def _make_encryptor() -> FieldEncryptor:
    return FieldEncryptor.from_master_secret("test-master-secret-32-bytes-long!")


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_set_then_get_round_trips_value() -> None:
    async with temp_mongo_database() as db:
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=_make_encryptor(),
        )
        repo = MongoTemplateVarRepository(scoped)
        await repo.set("default", "github", "TOKEN", "secret-value-1234")
        assert await repo.get_value("default", "github", "TOKEN") == "secret-value-1234"


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_value_is_encrypted_at_rest() -> None:
    """Critical invariant: the raw doc on disk holds ``enc:v1:...``."""
    async with temp_mongo_database() as db:
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=_make_encryptor(),
        )
        repo = MongoTemplateVarRepository(scoped)
        plaintext = "do-not-leak-this-token-in-the-DB"
        await repo.set("default", "github", "TOKEN", plaintext)
        # Bypass the wrapper to read the raw doc.
        raw = await db[COLL_TEMPLATE_VARS].find_one({"name": "TOKEN"})
        assert raw is not None
        stored = raw["value"]
        assert isinstance(stored, str)
        assert stored.startswith("enc:v1:")
        assert plaintext not in stored


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_list_summaries_returns_value_for_password_rows() -> None:
    async with temp_mongo_database() as db:
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=_make_encryptor(),
        )
        repo = MongoTemplateVarRepository(scoped)
        await repo.set("default", "github", "TOKEN", "x" * 32)
        summaries = await repo.list_summaries("default", "github")
        assert len(summaries) == 1
        assert summaries[0].name == "TOKEN"
        assert summaries[0].last_four == "xxxx"
        # The list path now carries the plaintext for password rows
        # too — the SPA obfuscates by default and exposes an eye
        # toggle. Encryption-at-rest still applies via the
        # OrgScopedCollection wrapper (decrypts on read).
        assert summaries[0].is_secret is True
        assert summaries[0].value == "x" * 32


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_delete_removes_doc() -> None:
    async with temp_mongo_database() as db:
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=_make_encryptor(),
        )
        repo = MongoTemplateVarRepository(scoped)
        await repo.set("default", "github", "TOKEN", "value")
        await repo.delete("default", "github", "TOKEN")
        assert await repo.get_value("default", "github", "TOKEN") is None


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_delete_all_scoped_to_upstream() -> None:
    async with temp_mongo_database() as db:
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=_make_encryptor(),
        )
        repo = MongoTemplateVarRepository(scoped)
        await repo.set("default", "github", "A", "v" * 32)
        await repo.set("default", "github", "B", "v" * 32)
        await repo.set("default", "notion", "C", "v" * 32)
        await repo.delete_all("default", "github")
        assert await repo.list_summaries("default", "github") == []
        assert len(await repo.list_summaries("default", "notion")) == 1


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_plain_var_value_is_still_encrypted_at_rest() -> None:
    """Defence in depth: even ``is_secret=False`` values are
    encrypted at rest (the field-level encryption is a
    collection-level invariant, not a per-doc decision)."""
    async with temp_mongo_database() as db:
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=_make_encryptor(),
        )
        repo = MongoTemplateVarRepository(scoped)
        plaintext = "plain-config-but-still-encrypted-at-rest"
        await repo.set(
            "default", "github", "LOG_LEVEL", plaintext, is_secret=False,
        )
        # Bypass the wrapper to read the raw doc.
        raw = await db[COLL_TEMPLATE_VARS].find_one({"name": "LOG_LEVEL"})
        assert raw is not None
        stored = raw["value"]
        assert isinstance(stored, str)
        assert stored.startswith("enc:v1:")
        assert plaintext not in stored
        # But the API path returns the value to the caller (since
        # ``is_secret=False``).
        summaries = await repo.list_summaries("default", "github")
        assert summaries[0].is_secret is False
        assert summaries[0].value == plaintext


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_replace_preserves_is_secret_flag() -> None:
    async with temp_mongo_database() as db:
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=_make_encryptor(),
        )
        repo = MongoTemplateVarRepository(scoped)
        await repo.set(
            "default", "github", "TOKEN", "first-value", is_secret=True,
        )
        summary = await repo.set(
            "default", "github", "TOKEN", "rotated", is_secret=False,
        )
        assert summary.is_secret is True
        assert summary.value == "rotated"


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_legacy_doc_without_is_secret_reads_as_secret() -> None:
    async with temp_mongo_database() as db:
        # Insert a v1-shaped doc by hand (no ``is_secret`` field).
        # Need to encrypt the value to match the storage contract.
        encryptor = _make_encryptor()
        await db[COLL_TEMPLATE_VARS].insert_one({
            "org_id": "default",
            "upstream_id": "github",
            "name": "LEGACY",
            "value": encryptor.encrypt_string("v1-value"),
            "last_four": "alue",
            "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
        })
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=encryptor,
        )
        repo = MongoTemplateVarRepository(scoped)
        summaries = await repo.list_summaries("default", "github")
        assert len(summaries) == 1
        assert summaries[0].is_secret is True
        # v1 docs stored the encrypted plaintext under ``value``;
        # the OrgScopedCollection wrapper decrypts it before our
        # repo's _summary_from_doc sees it, so it surfaces in the
        # list (under the new "always include value" contract).
        assert summaries[0].value == "v1-value"


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_per_org_isolation_via_org_scoped_collection() -> None:
    async with temp_mongo_database() as db:
        scoped = OrgScopedCollection(
            db[COLL_TEMPLATE_VARS], COLL_TEMPLATE_VARS, encryptor=_make_encryptor(),
        )
        repo = MongoTemplateVarRepository(scoped)
        await repo.set("orgA", "github", "TOKEN", "value-a")
        await repo.set("orgB", "github", "TOKEN", "value-b")
        assert await repo.get_value("orgA", "github", "TOKEN") == "value-a"
        assert await repo.get_value("orgB", "github", "TOKEN") == "value-b"
