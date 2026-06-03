"""Verify that tokens hit Mongo as ciphertext, not plaintext.

Writes an OAuth token through the standard ``MongoConnectionRepository``
API, then reads the raw Mongo document (bypassing ``OrgScopedCollection``)
and asserts that the ``access_token`` field in the stored doc is NOT
the plaintext value — it must be an ``enc:v1:...`` blob.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONNECTIONS,
    ENCRYPTED_FIELDS,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_connection_repository import (
    MongoConnectionRepository,
    _oauth_metadata_key,
    _user_key,
)
from mcpolis.adapters.repositories.mongo_oauth_state_repository import (
    MongoOAuthStateRepository,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateSnapshot,
    StoredAccessToken,
)
from tests.unit.mongo_fixture import temp_mongo_database

pytestmark = pytest.mark.asyncio

PLAINTEXT_TOKEN = "super-secret-access-token-plaintext"
PLAINTEXT_REFRESH = "super-secret-refresh-token-plaintext"


async def test_connection_tokens_encrypted_in_mongo() -> None:
    async with temp_mongo_database() as db:
        encryptor = FieldEncryptor.from_master_secret("cloud-master")
        coll = OrgScopedCollection(
            db[COLL_CONNECTIONS], COLL_CONNECTIONS, encryptor=encryptor,
        )
        repo = MongoConnectionRepository(coll)

        await repo.put_user_token(
            DEFAULT_ORG_ID,
            "alice@co.com",
            "github",
            OAuthToken(
                access_token=PLAINTEXT_TOKEN,
                refresh_token=PLAINTEXT_REFRESH,
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                scopes=["repo"],
            ),
        )

        # Raw Mongo read — no decryption path.
        raw = await db[COLL_CONNECTIONS].find_one(
            {"key": _user_key("alice@co.com", "github")}
        )
        assert raw is not None
        token_doc = raw["token"]
        assert token_doc["access_token"].startswith("enc:v1:")
        assert token_doc["refresh_token"].startswith("enc:v1:")
        assert PLAINTEXT_TOKEN not in token_doc["access_token"]
        assert PLAINTEXT_REFRESH not in token_doc["refresh_token"]

        # Read through the repo should still return plaintext.
        round_trip = await repo.get_user_token(
            DEFAULT_ORG_ID, "alice@co.com", "github",
        )
        assert round_trip is not None
        assert round_trip.access_token == PLAINTEXT_TOKEN
        assert round_trip.refresh_token == PLAINTEXT_REFRESH


async def test_oauth_metadata_stored_unencrypted_in_mongo() -> None:
    """RFC 8414 ``OAuthMetadata`` is purely public discovery data
    (issuer, token_endpoint, …) — every upstream serves it from a
    well-known URL. Encrypting it would burn KMS CPU for zero benefit
    and add a decryption-failure mode that takes a connection offline
    on key rotation. Pin that the field stays plaintext, AND that
    no entry leaks into ``ENCRYPTED_FIELDS[COLL_CONNECTIONS]``: a
    future refactor that "tightens up" encryption without checking
    sensitivity would otherwise silently flip this."""
    metadata = {
        "issuer": "https://oauth.example.invalid",
        "authorization_endpoint": "https://oauth.example.invalid/authorize",
        "token_endpoint": "https://oauth.example.invalid/oauth/token",
        "registration_endpoint": "https://oauth.example.invalid/register",
    }

    # Static check: no oauth_metadata.* path is in the encryption list.
    encrypted_paths = ENCRYPTED_FIELDS[COLL_CONNECTIONS]
    assert not any(p.startswith("oauth_metadata") for p in encrypted_paths), (
        f"oauth_metadata is public discovery data; should not be encrypted. "
        f"Got: {encrypted_paths}"
    )

    async with temp_mongo_database() as db:
        encryptor = FieldEncryptor.from_master_secret("cloud-master")
        coll = OrgScopedCollection(
            db[COLL_CONNECTIONS], COLL_CONNECTIONS, encryptor=encryptor,
        )
        repo = MongoConnectionRepository(coll)

        await repo.put_oauth_metadata(
            DEFAULT_ORG_ID, "mixpanel", "alice@co.com", metadata,
        )

        # Raw Mongo read — no decryption path. The persisted blob
        # should be byte-identical to what we wrote.
        raw = await db[COLL_CONNECTIONS].find_one(
            {"key": _oauth_metadata_key("mixpanel", "alice@co.com")}
        )
        assert raw is not None
        stored = raw["oauth_metadata"]
        assert stored == metadata, (
            f"oauth_metadata leaked through encryption layer: {stored}"
        )
        # Belt-and-suspenders: a value would start with "enc:v1:" if
        # something inadvertently routed it through the encryptor.
        for value in stored.values():
            assert not (
                isinstance(value, str) and value.startswith("enc:v1:")
            ), f"unexpected ciphertext in oauth_metadata: {value}"


async def test_oauth_state_snapshot_encrypted_in_mongo() -> None:
    """Whole-snapshot encryption: every sensitive field (client
    secrets, access tokens, refresh tokens, user emails) is protected
    at rest by encrypting the serialized JSON blob, not by enumerating
    individual fields. Pin that the plaintext email never reaches the
    raw Mongo document."""
    async with temp_mongo_database() as db:
        encryptor = FieldEncryptor.from_master_secret("cloud-master")
        coll = OrgScopedCollection(
            db["oauth_state"], "oauth_state", encryptor=encryptor,
        )
        repo = MongoOAuthStateRepository(coll, encryptor)

        snapshot = OAuthStateSnapshot()
        snapshot.access_tokens["tok"] = StoredAccessToken(
            token="tok",
            client_id="client-1",
            user_email="alice@co.com",
            scopes=["read"],
            expires_at=9999999999,
        )
        await repo.save(snapshot)

        # Single global document, keyed under DEFAULT_ORG_ID by the
        # repo (the OrgScopedCollection wrapper requires a key; the
        # OAuth state is logically global so we reuse DEFAULT_ORG_ID
        # as the constant key rather than building a separate
        # non-org-scoped wrapper).
        raw = await db["oauth_state"].find_one({"org_id": DEFAULT_ORG_ID})
        assert raw is not None
        blob = raw["encrypted_payload"]
        assert isinstance(blob, str)
        # Whole payload is encrypted; the plaintext email must not
        # appear in the raw document at all.
        assert "alice@co.com" not in blob
        assert blob.startswith("enc:v1:")

        # Round-trip restores the snapshot.
        loaded = await repo.load()
        assert "tok" in loaded.access_tokens
        assert loaded.access_tokens["tok"].user_email == "alice@co.com"


async def test_oauth_state_round_trips_through_global_document() -> None:
    """Gateway OAuth state is global, not partitioned by org —
    successive ``save`` calls overwrite the single document. Pin both
    that the collection only ever holds one document for the gateway
    snapshot AND that the second save replaces (not merges) the first.

    The org dimension is resolved per request from the URL slug or the
    user's memberships; tokens themselves are user-scoped. This test
    is the storage-layer counterpart to that contract — a future
    refactor that re-introduces per-org partitioning would have to
    break this test first.
    """
    async with temp_mongo_database() as db:
        encryptor = FieldEncryptor.from_master_secret("cloud-master")
        coll = OrgScopedCollection(
            db["oauth_state"], "oauth_state", encryptor=encryptor,
        )
        repo = MongoOAuthStateRepository(coll, encryptor)

        snap_first = OAuthStateSnapshot()
        snap_first.access_tokens["tok-a"] = StoredAccessToken(
            token="tok-a",
            client_id="client-a",
            user_email="alice@co.com",
            scopes=[],
            expires_at=9999999999,
        )
        snap_second = OAuthStateSnapshot()
        snap_second.access_tokens["tok-b"] = StoredAccessToken(
            token="tok-b",
            client_id="client-b",
            user_email="bob@co.com",
            scopes=[],
            expires_at=9999999999,
        )

        await repo.save(snap_first)
        await repo.save(snap_second)

        # Single global doc — not one per save.
        docs = await db["oauth_state"].find({}).to_list(length=None)
        assert len(docs) == 1, (
            f"expected one global oauth_state doc, got {len(docs)}: {docs}"
        )

        # Replace, not merge: the second save fully overwrites the
        # first. ``McpGatewayOAuthProvider`` always saves a complete
        # snapshot (the in-memory map is the source of truth), so a
        # merge semantic would silently keep stale tokens that the
        # provider has already evicted in memory.
        loaded = await repo.load()
        assert "tok-a" not in loaded.access_tokens
        assert "tok-b" in loaded.access_tokens
        assert loaded.access_tokens["tok-b"].user_email == "bob@co.com"
