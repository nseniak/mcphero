"""In-memory ``ToolCatalogRepository`` for unit tests.

Top-level helpers only — per CLAUDE.md, tests don't use fixtures and
factor common setup into ``make_*`` functions. Construct one of these
explicitly when a test needs to verify catalog persistence semantics.
"""
from __future__ import annotations

from mcpolis.domain.ports.tool_catalog_repository import (
    ToolCatalogRepository,
    ToolCatalogSnapshot,
)


class InMemoryToolCatalogStore(ToolCatalogRepository):
    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str], ToolCatalogSnapshot] = {}

    async def load_all(
        self, org_id: str,
    ) -> dict[str, ToolCatalogSnapshot]:
        return {
            upstream_id: snap
            for (org, upstream_id), snap in self._snapshots.items()
            if org == org_id
        }

    async def upsert_upstream(
        self,
        org_id: str,
        upstream_id: str,
        snapshot: ToolCatalogSnapshot,
    ) -> None:
        self._snapshots[(org_id, upstream_id)] = snapshot

    async def delete_upstream(
        self, org_id: str, upstream_id: str,
    ) -> None:
        self._snapshots.pop((org_id, upstream_id), None)


def make_tool_catalog_store() -> InMemoryToolCatalogStore:
    return InMemoryToolCatalogStore()
