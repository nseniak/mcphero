"""Mongo client lifecycle + ``OrgScopedCollection`` wrapper.

Phase 2c introduces Mongo as an alternative storage backend for cloud
mode. Every Mongo read / write MUST go through ``OrgScopedCollection``:
it injects the current ``org_id`` into every filter, encrypts sensitive
fields on write, and decrypts them on read. Domain code never sees
ciphertext, a raw collection, or a missing org filter.

Architectural enforcement: an architectural test under ``tests/`` greps
the ``mongo_*.py`` adapter files for bare calls like ``.find(`` or
``.insert_one(`` that are not routed through ``OrgScopedCollection``
and fails the build if any slip in.
"""
from __future__ import annotations

from typing import Any

import structlog
from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)
from pymongo import ASCENDING, DESCENDING

# Motor is generic over the document type. We always store regular
# ``dict[str, Any]`` BSON documents, so alias the parameterized types
# once and reuse them throughout the module.
MotorDatabase = AsyncIOMotorDatabase[dict[str, Any]]
MotorCollection = AsyncIOMotorCollection[dict[str, Any]]
MotorClient = AsyncIOMotorClient[dict[str, Any]]

from mcpolis.adapters.repositories.encryption import (
    FieldEncryptor,
    decrypt_fields,
    encrypt_fields,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Collection names used by the Mongo repositories. Centralized so the
# index-creation routine and the per-collection encryption allowlist
# agree on a single source of truth.
COLL_ORGANIZATIONS = "organizations"
COLL_MEMBERSHIPS = "memberships"
COLL_CONFIG = "config"
COLL_UPSTREAMS = "upstreams"
COLL_CONNECTIONS = "connections"
COLL_AUDIT = "audit"
COLL_OAUTH_STATE = "oauth_state"
COLL_LOCKS = "locks"
COLL_TOOL_CATALOG = "tool_catalog"
COLL_SANDBOX_REFS = "sandbox_refs"
COLL_TEMPLATE_VARS = "mcp_template_vars"
COLL_SANDBOX_FILES = "mcp_sandbox_files"
# No ENCRYPTED_FIELDS entry: only the sha256 hash of the token is
# stored (preimage-resistant over 256-bit random input), and the
# metadata matches the sensitivity of ``config.users``.
COLL_SERVICE_TOKENS = "service_tokens"

# Per-collection encrypted field allowlist. Each list entry is a dotted
# path inside a document. Anything not listed here is stored in plain
# text. The Mongo repositories rely on this mapping via
# ``OrgScopedCollection`` so that encryption is enforced centrally and
# a single new secret field only needs to be declared in one place.
ENCRYPTED_FIELDS: dict[str, list[str]] = {
    COLL_CONNECTIONS: [
        "token.access_token",
        "token.refresh_token",
        "client_info.client_id",
        "client_info.client_secret",
        "client_info.registration_access_token",
    ],
    COLL_OAUTH_STATE: [
        # Gateway OAuth state is stored as a single snapshot doc per
        # org with an ``encrypted_payload`` field. The whole payload is
        # encrypted as one blob (see ``mongo_oauth_state_repository``).
        "encrypted_payload",
    ],
    COLL_TEMPLATE_VARS: [
        # Per-MCP secrets — the plaintext value is encrypted at rest.
        # ``last_four`` and timestamps stay plaintext so the listing
        # path never needs to decrypt.
        "value",
    ],
    COLL_SANDBOX_FILES: [
        # Per-MCP uploaded credential / config files — the plaintext
        # contents are encrypted at rest. ``sha256``, ``size_bytes``,
        # ``target_path``, and timestamps stay plaintext so the
        # listing path never needs to decrypt the body.
        "contents",
    ],
    COLL_UPSTREAMS: [
        # User-supplied upstream config carries credentials in many
        # places: stdio command/args/env, http url/headers, OAuth
        # client_id/client_secret, scopes. We don't enumerate the
        # leaves — the repo serializes ``server_config`` and
        # ``options`` to JSON and stores each as a single encrypted
        # blob, so any new sensitive field added later is protected
        # automatically. ``upstream_id`` and the per-document
        # ``options.display_name`` / ``options.auth_mode`` /
        # ``options.transport_kind`` (when present at the top of the
        # doc) stay plaintext for listing without decryption.
        "server_config_encrypted",
        "options_encrypted",
    ],
}


class OrgScopedCollection:
    """Read/write access to a Mongo collection, always scoped to an org.

    ``OrgScopedCollection`` is the ONLY class in the Mongo adapter layer
    that may touch a raw motor collection. Repositories construct one
    per (collection, encryptor, collection_name) and use it exclusively.

    Every method merges ``{"org_id": org_id}`` into the filter on its
    way to Mongo. The wrapper cannot be used to query another org's
    data because it is impossible to omit ``org_id`` from the call
    signature, and the wrapper overwrites any ``org_id`` the caller
    might accidentally pre-populate.

    Encryption is transparent:
    - ``insert_one`` / ``replace_one`` / ``update_one``: fields listed
      in ``ENCRYPTED_FIELDS[collection_name]`` are encrypted before the
      write reaches motor.
    - ``find_one`` / ``find``: those same fields are decrypted on the
      way back.
    """

    def __init__(
        self,
        collection: MotorCollection,
        collection_name: str,
        encryptor: FieldEncryptor | None = None,
    ) -> None:
        self._raw = collection
        self._name = collection_name
        self._encryptor = encryptor
        self._enc_fields = ENCRYPTED_FIELDS.get(collection_name, [])

    # --- Internal helpers ---

    def _scope(self, org_id: str, filter_: dict[str, Any] | None) -> dict[str, Any]:
        scoped: dict[str, Any] = dict(filter_) if filter_ else {}
        scoped["org_id"] = org_id
        return scoped

    def _encrypt(self, doc: dict[str, Any]) -> dict[str, Any]:
        if self._encryptor is None or not self._enc_fields:
            return doc
        return encrypt_fields(doc, self._enc_fields, self._encryptor)

    def _decrypt(self, doc: dict[str, Any]) -> dict[str, Any]:
        if self._encryptor is None or not self._enc_fields:
            return doc
        return decrypt_fields(doc, self._enc_fields, self._encryptor)

    # --- CRUD ---

    async def find_one(
        self, org_id: str, filter_: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        doc = await self._raw.find_one(self._scope(org_id, filter_))
        if doc is None:
            return None
        return self._decrypt(doc)

    async def find_many(
        self,
        org_id: str,
        filter_: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int = 0,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        cursor = self._raw.find(self._scope(org_id, filter_))
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        docs: list[dict[str, Any]] = await cursor.to_list(length=None)
        return [self._decrypt(d) for d in docs]

    async def find_many_cross_org(
        self,
        filter_: dict[str, Any] | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int = 0,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        """Read across every org. **Operator-only** — callers MUST gate.

        The wrapper exists to enforce per-org isolation; this method is
        the deliberate, named exception used by the superadmin
        dashboard's cross-org views (audit log etc.). Documents are
        decrypted on the way back, and ``org_id`` is preserved so the
        caller can attribute each row.
        """
        cursor = self._raw.find(filter_ or {})
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        docs: list[dict[str, Any]] = await cursor.to_list(length=None)
        return [self._decrypt(d) for d in docs]

    async def insert_one(self, org_id: str, doc: dict[str, Any]) -> None:
        prepared = dict(doc)
        prepared["org_id"] = org_id
        prepared = self._encrypt(prepared)
        await self._raw.insert_one(prepared)

    async def replace_one(
        self,
        org_id: str,
        filter_: dict[str, Any],
        doc: dict[str, Any],
        *,
        upsert: bool = True,
    ) -> None:
        scoped_filter = self._scope(org_id, filter_)
        prepared = dict(doc)
        prepared["org_id"] = org_id
        prepared = self._encrypt(prepared)
        await self._raw.replace_one(scoped_filter, prepared, upsert=upsert)

    async def update_one(
        self,
        org_id: str,
        filter_: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> None:
        scoped_filter = self._scope(org_id, filter_)
        # Encryption for $set paths would require re-implementing path
        # resolution against operator prefixes. The repositories only
        # use ``update_one`` for non-secret field mutations; anything
        # that touches an encrypted field goes through ``replace_one``
        # instead. Enforce that expectation at runtime.
        if self._enc_fields and "$set" in update:
            for enc_path in self._enc_fields:
                top = enc_path.split(".", 1)[0]
                if top in update["$set"]:
                    raise RuntimeError(
                        f"OrgScopedCollection.update_one cannot set "
                        f"encrypted path {enc_path!r}; use replace_one"
                    )
        await self._raw.update_one(scoped_filter, update, upsert=upsert)

    async def delete_one(
        self, org_id: str, filter_: dict[str, Any]
    ) -> int:
        scoped_filter = self._scope(org_id, filter_)
        result = await self._raw.delete_one(scoped_filter)
        return int(result.deleted_count)

    async def delete_many(
        self, org_id: str, filter_: dict[str, Any]
    ) -> int:
        scoped_filter = self._scope(org_id, filter_)
        result = await self._raw.delete_many(scoped_filter)
        return int(result.deleted_count)

    async def count(
        self, org_id: str, filter_: dict[str, Any] | None = None
    ) -> int:
        return int(await self._raw.count_documents(self._scope(org_id, filter_)))

    async def distinct(
        self, org_id: str, field: str, filter_: dict[str, Any] | None = None
    ) -> list[Any]:
        result = await self._raw.distinct(field, self._scope(org_id, filter_))
        return list(result)

    # --- Raw access for bulk ops that still need org filtering ---

    async def bulk_replace_where(
        self,
        org_id: str,
        filter_: dict[str, Any],
        replacements: list[dict[str, Any]],
    ) -> None:
        """Delete all docs matching ``filter_`` in the org, then insert
        ``replacements`` (each carrying its own unique fields). Used by
        the OAuth state repo to atomically refresh the snapshot."""
        scoped_filter = self._scope(org_id, filter_)
        await self._raw.delete_many(scoped_filter)
        if not replacements:
            return
        prepared = [
            self._encrypt({**d, "org_id": org_id}) for d in replacements
        ]
        await self._raw.insert_many(prepared)


# ---------------------------------------------------------------------------
# Client lifecycle + index creation
# ---------------------------------------------------------------------------


class MongoConnection:
    """Owns the motor client and exposes the database handle.

    A single instance lives on the FastAPI app state in cloud mode and
    is closed during shutdown. Repositories do not own the client —
    they only see ``OrgScopedCollection`` instances.
    """

    def __init__(self, uri: str, db_name: str) -> None:
        self._client: MotorClient = AsyncIOMotorClient(uri)
        self._db_name = db_name

    @property
    def database(self) -> MotorDatabase:
        return self._client[self._db_name]

    def close(self) -> None:
        self._client.close()


async def create_indexes(
    db: MotorDatabase, *, audit_retention_days: int = 7,
) -> None:
    """Create the indexes required by the Mongo repositories.

    Called once at startup in cloud mode. Idempotent — ``create_index``
    on an existing index is a no-op. If the TTL window on the audit
    collection changes, the existing index is adjusted via ``collMod``
    so the new retention takes effect without manual intervention.
    """
    # Organizations: unique slug.
    await db[COLL_ORGANIZATIONS].create_index(
        [("slug", ASCENDING)], unique=True, name="uniq_slug"
    )

    # Memberships: (org_id, email) is a natural primary key.
    await db[COLL_MEMBERSHIPS].create_index(
        [("org_id", ASCENDING), ("email", ASCENDING)],
        unique=True,
        name="uniq_org_email",
    )
    await db[COLL_MEMBERSHIPS].create_index(
        [("email", ASCENDING)], name="by_email"
    )

    # Settings config: one doc per org.
    await db[COLL_CONFIG].create_index(
        [("org_id", ASCENDING)], unique=True, name="uniq_org"
    )

    # Upstreams: one doc per (org, upstream_id).
    await db[COLL_UPSTREAMS].create_index(
        [("org_id", ASCENDING), ("upstream_id", ASCENDING)],
        unique=True,
        name="uniq_org_upstream",
    )

    # Connections: generic key/value store per org, keyed by a synthetic key.
    await db[COLL_CONNECTIONS].create_index(
        [("org_id", ASCENDING), ("key", ASCENDING)],
        unique=True,
        name="uniq_org_key",
    )

    # Audit: time-ordered queries within an org, plus filter helpers.
    await db[COLL_AUDIT].create_index(
        [("org_id", ASCENDING), ("timestamp", DESCENDING)],
        name="by_org_time",
    )
    await db[COLL_AUDIT].create_index(
        [("org_id", ASCENDING), ("user_id", ASCENDING)],
        name="by_org_user",
    )
    await db[COLL_AUDIT].create_index(
        [("org_id", ASCENDING), ("upstream_id", ASCENDING)],
        name="by_org_upstream",
    )
    # Audit retention: Mongo TTL on ``created_at`` auto-deletes docs
    # older than ``audit_retention_days``. The ``timestamp`` field is an
    # ISO string and cannot drive TTL, so MongoAuditRepository writes a
    # BSON Date ``created_at`` alongside it.
    await _ensure_ttl_index(
        db[COLL_AUDIT],
        index_name="ttl_created_at",
        keys=[("created_at", ASCENDING)],
        expire_after_seconds=audit_retention_days * 86400,
    )

    # OAuth state: one snapshot doc per org.
    await db[COLL_OAUTH_STATE].create_index(
        [("org_id", ASCENDING)], unique=True, name="uniq_org"
    )

    # Tool catalog: one doc per (org, upstream_id), persisted so the
    # admin permissions UI works across restarts without an OAuth
    # reconnect.
    await db[COLL_TOOL_CATALOG].create_index(
        [("org_id", ASCENDING), ("upstream_id", ASCENDING)],
        unique=True,
        name="uniq_org_upstream",
    )

    # Distributed locks: TTL auto-expiry on expires_at.
    await db[COLL_LOCKS].create_index(
        [("expires_at", ASCENDING)],
        expireAfterSeconds=0,
        name="ttl_expires",
    )

    # Sandbox refs: one doc per (org_id, upstream_id) tracking the
    # current sandbox / paused snapshot so the in-memory state
    # registry can be reconstructed across crashes. The mcpolis_instance
    # field is queried by the per-backend reconciler so we add a
    # secondary index on it for the cross-org sweep.
    await db[COLL_SANDBOX_REFS].create_index(
        [("org_id", ASCENDING), ("upstream_id", ASCENDING)],
        unique=True,
        name="uniq_org_upstream",
    )
    await db[COLL_SANDBOX_REFS].create_index(
        [("mcpolis_instance", ASCENDING)],
        name="by_instance",
    )

    # MCP secrets: one doc per (org, upstream, name).
    await db[COLL_TEMPLATE_VARS].create_index(
        [
            ("org_id", ASCENDING),
            ("upstream_id", ASCENDING),
            ("name", ASCENDING),
        ],
        unique=True,
        name="uniq_org_upstream_name",
    )

    # Sandbox files: one doc per (org, upstream, name). Same composite
    # key as template vars — the two share a ``${...}`` namespace and
    # collisions are rejected at write time, but the unique index here
    # is a per-collection invariant (uniqueness across collections is
    # enforced by the admin route).
    await db[COLL_SANDBOX_FILES].create_index(
        [
            ("org_id", ASCENDING),
            ("upstream_id", ASCENDING),
            ("name", ASCENDING),
        ],
        unique=True,
        name="uniq_org_upstream_name",
    )

    # Service tokens: hash is the global verify-path lookup key;
    # (org_id, label) is the admin-facing identity key.
    await db[COLL_SERVICE_TOKENS].create_index(
        [("token_hash", ASCENDING)],
        unique=True,
        name="uniq_token_hash",
    )
    await db[COLL_SERVICE_TOKENS].create_index(
        [("org_id", ASCENDING), ("label", ASCENDING)],
        unique=True,
        name="uniq_org_label",
    )

    logger.info("mongo.indexes.created")


async def _ensure_ttl_index(
    collection: MotorCollection,
    *,
    index_name: str,
    keys: list[tuple[str, int]],
    expire_after_seconds: int,
) -> None:
    """Create or adjust a TTL index to the requested expiry.

    MongoDB rejects ``create_index`` if an index with the same name
    already exists with a different ``expireAfterSeconds``. This helper
    detects that mismatch and uses ``collMod`` to update the TTL in
    place rather than failing startup.
    """
    existing = await collection.index_information()
    desired = int(expire_after_seconds)
    info = existing.get(index_name)
    if info is not None and info.get("expireAfterSeconds") == desired:
        return
    if info is not None:
        await collection.database.command(
            {
                "collMod": collection.name,
                "index": {
                    "name": index_name,
                    "expireAfterSeconds": desired,
                },
            }
        )
        return
    await collection.create_index(
        keys, expireAfterSeconds=desired, name=index_name,
    )
