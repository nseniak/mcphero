"""Verify that tokens hit Mongo as ciphertext, not plaintext.

Writes an OAuth token through the standard ``MongoConnectionRepository``
API, then reads the raw Mongo document (bypassing ``OrgScopedCollection``)
and asserts that the ``access_token`` field in the stored doc is NOT
the plaintext value — it must be an ``enc:v1:...`` blob.

The gateway sign-in store's encryption at rest is pinned in
``test_gateway_oauth_storage.py``.
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
from mcpolis.domain.ports import DEFAULT_ORG_ID
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

