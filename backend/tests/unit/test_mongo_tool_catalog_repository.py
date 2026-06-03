"""Phase 0 — Mongo adapter contract for ``ToolCatalogRepository``.

Same shape as the file-store contract test but against a real Mongo
instance via ``temp_mongo_database`` (skipped when Mongo is not
reachable). All reads/writes go through ``OrgScopedCollection`` so
org isolation is exercised by the test as well.
"""
from __future__ import annotations

import pytest

from mcpolis.adapters.repositories.mongo_client import (
    COLL_TOOL_CATALOG,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_tool_catalog_repository import (
    MongoToolCatalogRepository,
)
from mcpolis.domain.model.upstream import DiscoveredTool
from mcpolis.domain.ports.tool_catalog_repository import ToolCatalogSnapshot

from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


def make_tool(upstream_id: str, name: str) -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id=upstream_id,
        original_name=name,
        prefixed_name=f"{upstream_id}__{name}",
        description=None,
        input_schema={"type": "object"},
    )


@pytest.mark.skipif(
    not mongo_available(), reason="Mongo not reachable",
)
@pytest.mark.asyncio
async def test_round_trip_via_mongo() -> None:
    async with temp_mongo_database() as db:
        repo = MongoToolCatalogRepository(
            OrgScopedCollection(db[COLL_TOOL_CATALOG], COLL_TOOL_CATALOG),
        )

        await repo.upsert_upstream(
            "default", "github",
            ToolCatalogSnapshot(tools=[make_tool("github", "create_issue")]),
        )

        snapshots = await repo.load_all("default")
        assert {t.original_name for t in snapshots["github"].tools} == {
            "create_issue",
        }


@pytest.mark.skipif(
    not mongo_available(), reason="Mongo not reachable",
)
@pytest.mark.asyncio
async def test_upsert_replaces_previous_doc() -> None:
    async with temp_mongo_database() as db:
        repo = MongoToolCatalogRepository(
            OrgScopedCollection(db[COLL_TOOL_CATALOG], COLL_TOOL_CATALOG),
        )

        await repo.upsert_upstream(
            "default", "github",
            ToolCatalogSnapshot(tools=[make_tool("github", "old")]),
        )
        await repo.upsert_upstream(
            "default", "github",
            ToolCatalogSnapshot(tools=[make_tool("github", "new")]),
        )

        snapshots = await repo.load_all("default")
        assert {t.original_name for t in snapshots["github"].tools} == {"new"}


@pytest.mark.skipif(
    not mongo_available(), reason="Mongo not reachable",
)
@pytest.mark.asyncio
async def test_delete_removes_only_one_upstream() -> None:
    async with temp_mongo_database() as db:
        repo = MongoToolCatalogRepository(
            OrgScopedCollection(db[COLL_TOOL_CATALOG], COLL_TOOL_CATALOG),
        )

        await repo.upsert_upstream(
            "default", "github",
            ToolCatalogSnapshot(tools=[make_tool("github", "create_issue")]),
        )
        await repo.upsert_upstream(
            "default", "notion",
            ToolCatalogSnapshot(tools=[make_tool("notion", "search")]),
        )

        await repo.delete_upstream("default", "github")

        snapshots = await repo.load_all("default")
        assert "github" not in snapshots
        assert "notion" in snapshots


@pytest.mark.skipif(
    not mongo_available(), reason="Mongo not reachable",
)
@pytest.mark.asyncio
async def test_orgs_isolated() -> None:
    async with temp_mongo_database() as db:
        repo = MongoToolCatalogRepository(
            OrgScopedCollection(db[COLL_TOOL_CATALOG], COLL_TOOL_CATALOG),
        )

        await repo.upsert_upstream(
            "org_a", "github",
            ToolCatalogSnapshot(tools=[make_tool("github", "tool_a")]),
        )
        await repo.upsert_upstream(
            "org_b", "github",
            ToolCatalogSnapshot(tools=[make_tool("github", "tool_b")]),
        )

        a = await repo.load_all("org_a")
        b = await repo.load_all("org_b")

        assert {t.original_name for t in a["github"].tools} == {"tool_a"}
        assert {t.original_name for t in b["github"].tools} == {"tool_b"}
