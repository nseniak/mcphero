"""Mongo-backed ``OAuthStateRepository``.

One document per item in the ``gateway_oauth`` collection::

    {org_id: "default",
     kind: "clients" | "access_tokens" | "refresh_tokens" | "client_approvals",
     key: <keyed hash of the item's key>,
     payload: <the item as JSON, AES-GCM-encrypted>}

So no document grows with the number of registrations or sign-ins, and
one item that cannot be written does not stop the others. The payload
holds the whole item, so *every* sensitive field (client secrets,
access and refresh tokens, user emails) is encrypted at rest without
enumerating them; ``key`` is ``FieldEncryptor.lookup_hash`` of the
client id, token or approval key, so no token or email is stored in
clear either.

The state is global, not partitioned by org: gateway tokens identify a
user, and the org dimension is resolved per request from the URL or
from the user's memberships. ``org_id`` is the constant
``DEFAULT_ORG_ID``, because ``OrgScopedCollection`` requires one.

Older builds kept the whole state in ONE document of the
``oauth_state`` collection. ``load`` converts that document once (see
``_convert_single_document``), and leaves it as it was: a rollback to
an older build still finds the sign-ins made before the conversion.
"""
from __future__ import annotations

import json
import time

import structlog
from pymongo.errors import DuplicateKeyError

from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.mongo_client import (
    COLL_OAUTH_STATE,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.oauth_state_codec import (
    ACCESS_TOKENS,
    CLIENT_APPROVALS,
    CLIENTS,
    REFRESH_TOKENS,
    ItemCodec,
    JsonObject,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    OAuthStateRepository,
    OAuthStateSnapshot,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_GLOBAL_KEY = DEFAULT_ORG_ID

# Keys per ``$in`` delete, so a large clean-up stays a small query.
_DELETE_BATCH = 500

# The document recording that the older single document was converted,
# in the same collection as the items. Its ``kind`` is no item kind, so
# ``load`` never reads it as an item, and no write or delete touches it.
_CONVERTED = {"kind": "conversion", "key": COLL_OAUTH_STATE}


class MongoOAuthStateRepository(OAuthStateRepository):
    def __init__(
        self,
        collection: OrgScopedCollection,
        single_document_collection: OrgScopedCollection,
        encryptor: FieldEncryptor,
    ) -> None:
        # ``collection`` encrypts each document's ``payload`` itself
        # (``ENCRYPTED_FIELDS``); the encryptor hashes the keys, and
        # removes the inner encryption layer of the older document.
        self._coll = collection
        self._single_document = single_document_collection
        self._encryptor = encryptor

    async def load(self) -> OAuthStateSnapshot:
        await self._convert_single_document()
        docs = await self._coll.find_many(
            _GLOBAL_KEY, on_unreadable=_skip_unreadable,
        )
        return OAuthStateSnapshot(
            clients=_read_items(CLIENTS, docs),
            access_tokens=_read_items(ACCESS_TOKENS, docs),
            refresh_tokens=_read_items(REFRESH_TOKENS, docs),
            client_approvals=_read_items(CLIENT_APPROVALS, docs),
        )

    async def apply(self, changes: OAuthStateChanges) -> None:
        await self._put(CLIENTS, changes.clients)
        await self._put(ACCESS_TOKENS, changes.access_tokens)
        await self._put(REFRESH_TOKENS, changes.refresh_tokens)
        await self._put(CLIENT_APPROVALS, changes.client_approvals)
        await self._delete(CLIENTS, changes.clients)
        await self._delete(ACCESS_TOKENS, changes.access_tokens)
        await self._delete(REFRESH_TOKENS, changes.refresh_tokens)
        await self._delete(CLIENT_APPROVALS, changes.client_approvals)

    def _doc_key(self, key: str) -> str:
        return self._encryptor.lookup_hash(key)

    def _document[T](self, codec: ItemCodec[T], key: str, item: T) -> JsonObject:
        return {
            "kind": codec.kind,
            "key": self._doc_key(key),
            "payload": json.dumps(codec.encode(item)),
        }

    async def _put[T](
        self, codec: ItemCodec[T], changes: dict[str, T | None],
    ) -> None:
        for key, item in changes.items():
            if item is None:
                continue
            await self._coll.replace_one(
                _GLOBAL_KEY,
                {"kind": codec.kind, "key": self._doc_key(key)},
                self._document(codec, key, item),
                upsert=True,
            )

    async def _delete[T](
        self, codec: ItemCodec[T], changes: dict[str, T | None],
    ) -> None:
        doc_keys = [
            self._doc_key(key) for key, item in changes.items() if item is None
        ]
        for start in range(0, len(doc_keys), _DELETE_BATCH):
            await self._coll.delete_many(
                _GLOBAL_KEY,
                {
                    "kind": codec.kind,
                    "key": {"$in": doc_keys[start:start + _DELETE_BATCH]},
                },
            )

    # --- One-time conversion of the older single document ---

    async def _convert_single_document(self) -> None:
        """Split the older single ``oauth_state`` document into one
        document per item, once, and record that it was (``_CONVERTED``).

        The single document is left as it was, for a rollback: an older
        build reads only that document, so deleting it would sign every
        gateway user out. Once recorded, the conversion never runs
        again, even if an older build has written the document since:
        running it again would bring back every token revoked since the
        first one. (So what an older build signs in after the
        conversion is not carried over; those people sign in again.)

        Safe if the process dies half-way: an item is only inserted when
        absent (the unique index on ``kind`` + ``key`` refuses a second
        copy), and the conversion is recorded only once every item is
        stored. Startup loads the state only after this returns. A
        failure raises, which stops the startup before the conversion
        is recorded.
        """
        if await self._coll.find_one(_GLOBAL_KEY, _CONVERTED) is not None:
            return
        doc = await self._single_document.find_one(_GLOBAL_KEY)
        if doc is None:
            return
        snapshot = self._read_single_document(doc)
        inserted = {
            CLIENTS.kind: await self._insert_missing(CLIENTS, snapshot.clients),
            ACCESS_TOKENS.kind: await self._insert_missing(
                ACCESS_TOKENS, snapshot.access_tokens,
            ),
            REFRESH_TOKENS.kind: await self._insert_missing(
                REFRESH_TOKENS, snapshot.refresh_tokens,
            ),
            CLIENT_APPROVALS.kind: await self._insert_missing(
                CLIENT_APPROVALS, snapshot.client_approvals,
            ),
        }
        try:
            await self._coll.insert_one(
                _GLOBAL_KEY, {**_CONVERTED, "converted_at": time.time()},
            )
        except DuplicateKeyError:
            pass  # another backend, converting at the same time, was first
        total = (
            len(snapshot.clients) + len(snapshot.access_tokens)
            + len(snapshot.refresh_tokens) + len(snapshot.client_approvals)
        )
        # No field name may contain "token": the log redactor masks
        # the value of any such field.
        logger.info(
            "gateway_oauth.single_document.converted",
            clients=len(snapshot.clients),
            access=len(snapshot.access_tokens),
            refresh=len(snapshot.refresh_tokens),
            client_approvals=len(snapshot.client_approvals),
            already_converted=total - sum(inserted.values()),
        )

    def _read_single_document(self, doc: JsonObject) -> OAuthStateSnapshot:
        # ``OrgScopedCollection`` removed its own encryption layer; the
        # older repository had encrypted the JSON once more itself.
        payload = doc.get("encrypted_payload")
        if not isinstance(payload, str):
            raise ValueError("the oauth_state document has no payload")
        raw: object = json.loads(self._encryptor.decrypt_string(payload))
        if not isinstance(raw, dict):
            raise ValueError("the oauth_state document is not a JSON object")
        data: JsonObject = raw  # pyright: ignore[reportUnknownVariableType]
        return OAuthStateSnapshot(
            clients=CLIENTS.decode_all(data.get(CLIENTS.kind)),
            access_tokens=ACCESS_TOKENS.decode_all(data.get(ACCESS_TOKENS.kind)),
            refresh_tokens=REFRESH_TOKENS.decode_all(data.get(REFRESH_TOKENS.kind)),
            client_approvals=CLIENT_APPROVALS.decode_all(
                data.get(CLIENT_APPROVALS.kind),
            ),
        )

    async def _insert_missing[T](
        self, codec: ItemCodec[T], items: dict[str, T],
    ) -> int:
        """Store each item unless already stored; returns how many
        were stored now."""
        inserted = 0
        for key, item in items.items():
            try:
                await self._coll.insert_one(
                    _GLOBAL_KEY, self._document(codec, key, item),
                )
            except DuplicateKeyError:
                continue
            inserted += 1
        return inserted


def _skip_unreadable(doc: JsonObject, error: Exception) -> None:
    """A stored item whose payload does not decrypt (written under
    another encryption key, cut short, edited by hand) is left in place
    and skipped: it costs its own sign-in, not the whole startup."""
    logger.error(
        "gateway_oauth.stored_item.unreadable",
        kind=doc.get("kind"),
        document_id=str(doc.get("_id")),
        error_type=type(error).__name__,
    )


def _read_items[T](codec: ItemCodec[T], docs: list[JsonObject]) -> dict[str, T]:
    """The items of one kind, keyed as ``OAuthStateSnapshot`` keys them.
    A document that does not parse is skipped (and logged)."""
    items: dict[str, T] = {}
    for doc in docs:
        if doc.get("kind") != codec.kind:
            continue
        payload = doc.get("payload")
        try:
            data: object = json.loads(payload) if isinstance(payload, str) else None
            item = codec.decode_or_none(data)
            if item is not None:
                items[codec.key_of(item)] = item
        except ValueError:  # bad JSON, or an item with no key
            logger.warning("gateway_oauth.stored_item.invalid", kind=codec.kind)
    return items
