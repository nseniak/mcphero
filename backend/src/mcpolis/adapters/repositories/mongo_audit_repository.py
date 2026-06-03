"""Mongo-backed ``AuditRepository`` for cloud mode."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from mcpolis.adapters.repositories.audit_repository import (
    AuditRepository as LegacyAuditRepository,
)
from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.events import Event
from mcpolis.domain.ports.audit_repository import AuditRepository
from mcpolis.domain.ports.event_stream import EventStream


class MongoAuditRepository(LegacyAuditRepository, AuditRepository):
    def __init__(
        self,
        collection: OrgScopedCollection,
        event_bus: EventStream | None = None,
    ) -> None:
        self._coll = collection
        self._event_bus = event_bus

    async def log(self, org_id: str, entry: AuditEntry) -> None:
        data = entry.model_dump()
        # org_id on the doc is already injected by OrgScopedCollection,
        # but the entry model also has an org_id field that may differ
        # from the effective scope. Drop the field-level org_id to avoid
        # double-storing it under different semantics.
        data.pop("org_id", None)
        # BSON Date field consumed by the TTL index on this collection.
        # The existing ``timestamp`` field is an ISO string, which TTL
        # indexes cannot use.
        data["created_at"] = datetime.now(UTC)
        await self._coll.insert_one(org_id, data)
        if self._event_bus is not None:
            self._event_bus.publish(
                org_id, Event(type="audit_entry", payload=entry.model_dump()),
            )

    async def search(
        self,
        org_id: str,
        user_id: str | None = None,
        mcp_id: str | None = None,
        tool: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        since_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        filter_: dict[str, Any] = {}
        if user_id:
            filter_["user_id"] = user_id
        if mcp_id:
            filter_["upstream_id"] = mcp_id
        if tool:
            # Case-insensitive substring match, matching the file-store
            # behavior. re.escape so a literal "." doesn't become a
            # wildcard and regex metacharacters can't be used for ReDoS.
            filter_["tool"] = {"$regex": re.escape(tool), "$options": "i"}
        if action:
            filter_["action"] = {"$in": action}
        if since_iso:
            # ``timestamp`` is stored as an ISO 8601 UTC string;
            # lexicographic ``$gte`` matches chronological ``>=``
            # for that format. The plan retention gate caps reads —
            # the global TTL still cleans rows physically.
            filter_["timestamp"] = {"$gte": since_iso}
        docs = await self._coll.find_many(
            org_id,
            filter_,
            sort=[("timestamp", -1)],
            limit=limit,
            skip=offset,
        )
        # Strip internal fields the file store never returned.
        cleaned: list[dict[str, Any]] = []
        for d in docs:
            clean = dict(d)
            clean.pop("_id", None)
            clean.pop("org_id", None)
            clean.pop("created_at", None)
            cleaned.append(clean)
        return cleaned

    async def search_cross_org(
        self,
        user_id: str | None = None,
        mcp_id: str | None = None,
        tool: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # Operator-only cross-org read. Bypasses ``OrgScopedCollection``
        # by going to the underlying motor collection directly — the
        # whole point of the route is to view across every tenant.
        # Callers MUST gate this on a superadmin check (the only call
        # site is ``/api/superadmin/audit``).
        filter_: dict[str, Any] = {}
        if user_id:
            filter_["user_id"] = user_id
        if mcp_id:
            filter_["upstream_id"] = mcp_id
        if tool:
            filter_["tool"] = {"$regex": re.escape(tool), "$options": "i"}
        if action:
            filter_["action"] = {"$in": action}
        docs = await self._coll.find_many_cross_org(
            filter_,
            sort=[("timestamp", -1)],
            limit=limit,
            skip=offset,
        )
        cleaned: list[dict[str, Any]] = []
        for d in docs:
            clean = dict(d)
            clean.pop("_id", None)
            clean.pop("created_at", None)
            # Keep ``org_id`` — the dashboard renders one row per
            # entry across orgs and needs to attribute each.
            cleaned.append(clean)
        return cleaned

    async def get_filter_values(self, org_id: str) -> dict[str, list[str]]:
        user_ids = await self._coll.distinct(org_id, "user_id")
        upstream_ids = await self._coll.distinct(org_id, "upstream_id")
        return {
            "user_ids": sorted(u for u in user_ids if isinstance(u, str)),
            "upstream_ids": sorted(u for u in upstream_ids if isinstance(u, str)),
        }
