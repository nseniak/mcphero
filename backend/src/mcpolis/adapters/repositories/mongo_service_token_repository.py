"""Mongo-backed service-token registry for cloud mode.

Takes the **plain** collection (not :class:`OrgScopedCollection`):
``get_by_hash`` runs on the auth path before any org context exists,
so the lookup is deliberately unscoped; the org-scoped methods apply
their ``org_id`` filter manually. No field encryption — nothing at
rest is secret (the hash is preimage-resistant over a 256-bit random
input; the metadata matches the sensitivity of ``config.users``).

Uniqueness of ``token_hash`` and ``(org_id, label)`` is enforced by
the indexes declared in ``mongo_client.create_indexes``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from mcpolis.adapters.repositories.mongo_client import MotorCollection
from mcpolis.domain.model.service_token import ServiceTokenRecord
from mcpolis.domain.ports.service_token_repository import (
    DuplicateServiceTokenLabelError,
    ServiceTokenRepository,
)


def _as_utc(value: datetime | None) -> datetime | None:
    # BSON dates are UTC but Motor hands them back naive.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _record_from_doc(doc: dict[str, Any]) -> ServiceTokenRecord:
    created_at = _as_utc(doc["created_at"])
    assert created_at is not None
    return ServiceTokenRecord(
        token_hash=doc["token_hash"],
        org_id=doc["org_id"],
        label=doc["label"],
        role_name=doc["role_name"],
        created_by=doc["created_by"],
        created_at=created_at,
        last_used_at=_as_utc(doc.get("last_used_at")),
    )


class MongoServiceTokenRepository(ServiceTokenRepository):
    def __init__(self, collection: MotorCollection) -> None:
        self._coll = collection

    async def create(self, record: ServiceTokenRecord) -> None:
        doc = {
            "token_hash": record.token_hash,
            "org_id": record.org_id,
            "label": record.label,
            "role_name": record.role_name,
            "created_by": record.created_by,
            "created_at": record.created_at,
            "last_used_at": record.last_used_at,
        }
        try:
            await self._coll.insert_one(doc)
        except DuplicateKeyError as exc:
            raise DuplicateServiceTokenLabelError(record.label) from exc

    async def get_by_hash(
        self, token_hash: str
    ) -> ServiceTokenRecord | None:
        doc = await self._coll.find_one({"token_hash": token_hash})
        if doc is None:
            return None
        return _record_from_doc(doc)

    async def list_for_org(self, org_id: str) -> list[ServiceTokenRecord]:
        cursor = self._coll.find({"org_id": org_id}).sort("label", 1)
        docs = await cursor.to_list(length=None)
        return [_record_from_doc(d) for d in docs]

    async def get_by_label(
        self, org_id: str, label: str
    ) -> ServiceTokenRecord | None:
        doc = await self._coll.find_one({"org_id": org_id, "label": label})
        if doc is None:
            return None
        return _record_from_doc(doc)

    async def delete_by_label(self, org_id: str, label: str) -> bool:
        result = await self._coll.delete_one(
            {"org_id": org_id, "label": label},
        )
        return result.deleted_count > 0

    async def delete_for_org(self, org_id: str) -> int:
        result = await self._coll.delete_many({"org_id": org_id})
        return result.deleted_count

    async def touch_last_used(
        self, token_hash: str, when: datetime
    ) -> None:
        await self._coll.update_one(
            {"token_hash": token_hash},
            {"$set": {"last_used_at": when}},
        )
