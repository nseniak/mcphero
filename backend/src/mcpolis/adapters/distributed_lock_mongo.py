"""Mongo-backed distributed lock for cloud (multi-backend) mode.

Uses a ``locks`` collection with a TTL index on ``expires_at`` so
expired locks are garbage-collected automatically by MongoDB.

acquire() uses findOneAndUpdate with upsert + $setOnInsert:
- If no doc exists → inserted with this holder → acquired.
- If a doc exists with the same holder → already held → acquired.
- If a doc exists with a different holder → lock is taken → not acquired.

release() deletes the doc only if the holder matches.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase


COLL_LOCKS = "locks"


class MongoDistributedLock:
    """TTL-based distributed lock backed by MongoDB."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:  # type: ignore[type-arg]
        self._coll: AsyncIOMotorCollection[dict[str, Any]] = db[COLL_LOCKS]
        self._holder = str(uuid.uuid4())

    async def acquire(self, key: str, ttl_seconds: float = 30) -> bool:
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        result = await self._coll.find_one_and_update(
            {"_id": key},
            {
                "$setOnInsert": {
                    "holder": self._holder,
                    "expires_at": expires,
                },
            },
            upsert=True,
            return_document=True,
        )
        if result is None:  # pyright: ignore[reportUnnecessaryComparison]
            return False
        if result["holder"] == self._holder:
            # We either just inserted or already held it — refresh TTL.
            await self._coll.update_one(
                {"_id": key, "holder": self._holder},
                {"$set": {"expires_at": expires}},
            )
            return True
        # Another holder has the lock.  Check if it's expired
        # (TTL cleanup may lag a few seconds).
        # Mongo may return naive datetimes — normalize to UTC-aware.
        stored_expires: datetime = result["expires_at"]
        if stored_expires.tzinfo is None:
            stored_expires = stored_expires.replace(tzinfo=UTC)
        if stored_expires < datetime.now(UTC):
            # Force-take the expired lock.
            replaced = await self._coll.find_one_and_update(
                {"_id": key, "holder": result["holder"]},
                {"$set": {"holder": self._holder, "expires_at": expires}},
                return_document=True,
            )
            return replaced is not None and replaced["holder"] == self._holder  # pyright: ignore[reportUnnecessaryComparison]
        return False

    async def release(self, key: str) -> None:
        await self._coll.delete_one({"_id": key, "holder": self._holder})

    async def close(self) -> None:
        pass
