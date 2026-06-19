from __future__ import annotations

import structlog

from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.domain.ports.tool_catalog_repository import (
    ToolCatalogRepository,
    ToolCatalogSnapshot,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class MongoToolCatalogRepository(ToolCatalogRepository):
    """Mongo-backed tool catalog persistence.

    One document per ``(org_id, upstream_id)`` in the ``tool_catalog``
    collection. ``OrgScopedCollection`` injects ``org_id`` on every
    read/write. No fields are encrypted: tool / resource / prompt
    metadata is the same content the upstream advertises publicly to
    every connected client; nothing in here is a secret.
    """

    def __init__(self, coll: OrgScopedCollection) -> None:
        self._coll = coll

    async def load_all(
        self, org_id: str,
    ) -> dict[str, ToolCatalogSnapshot]:
        docs = await self._coll.find_many(org_id)
        out: dict[str, ToolCatalogSnapshot] = {}
        for doc in docs:
            upstream_id = doc.get("upstream_id")
            if not isinstance(upstream_id, str):
                continue
            try:
                out[upstream_id] = ToolCatalogSnapshot.model_validate(
                    doc.get("snapshot", {}),
                )
            except Exception:
                logger.warning(
                    "tool_catalog_repository.deserialize.failed",
                    org_id=org_id,
                    upstream_id=upstream_id,
                )
        return out

    async def upsert_upstream(
        self,
        org_id: str,
        upstream_id: str,
        snapshot: ToolCatalogSnapshot,
    ) -> None:
        await self._coll.replace_one(
            org_id,
            {"upstream_id": upstream_id},
            {
                "upstream_id": upstream_id,
                "snapshot": snapshot.model_dump(mode="json"),
            },
            upsert=True,
        )

    async def delete_upstream(
        self, org_id: str, upstream_id: str,
    ) -> None:
        await self._coll.delete_one(
            org_id, {"upstream_id": upstream_id},
        )

    async def delete_all_for_org(self, org_id: str) -> None:
        # Org-deletion cascade. ``OrgScopedCollection`` scopes the empty
        # filter to this org, so no other tenant's snapshots are touched.
        await self._coll.delete_many(org_id, {})
