"""Phase 0 — file adapter contract for ``ToolCatalogRepository``.

The standalone-mode adapter writes one JSON file per org under
``<data_dir>/<org_id>/tool_catalog.json``. These tests exercise the
round-trip behavior, isolation between orgs, and cold-start behavior
when the file does not yet exist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_tool_catalog_store import (
    FileToolCatalogStore,
)
from mcpolis.domain.model.upstream import DiscoveredTool
from mcpolis.domain.ports.tool_catalog_repository import ToolCatalogSnapshot


def make_tool(upstream_id: str, name: str) -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id=upstream_id,
        original_name=name,
        prefixed_name=f"{upstream_id}__{name}",
        description=None,
        input_schema={"type": "object"},
    )


@pytest.mark.asyncio
async def test_load_all_returns_empty_when_no_file(tmp_path: Path) -> None:
    store = FileToolCatalogStore(tmp_path)
    assert await store.load_all("default") == {}


@pytest.mark.asyncio
async def test_upsert_then_load_round_trips(tmp_path: Path) -> None:
    store = FileToolCatalogStore(tmp_path)
    snap = ToolCatalogSnapshot(
        tools=[make_tool("github", "create_issue")],
    )

    await store.upsert_upstream("default", "github", snap)
    snapshots = await store.load_all("default")

    assert "github" in snapshots
    assert {t.original_name for t in snapshots["github"].tools} == {
        "create_issue",
    }


@pytest.mark.asyncio
async def test_upsert_replaces_previous_snapshot(tmp_path: Path) -> None:
    store = FileToolCatalogStore(tmp_path)
    await store.upsert_upstream(
        "default", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "old")]),
    )
    await store.upsert_upstream(
        "default", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "new")]),
    )

    snapshots = await store.load_all("default")
    assert {t.original_name for t in snapshots["github"].tools} == {"new"}


@pytest.mark.asyncio
async def test_delete_upstream_removes_only_that_upstream(
    tmp_path: Path,
) -> None:
    store = FileToolCatalogStore(tmp_path)
    await store.upsert_upstream(
        "default", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "create_issue")]),
    )
    await store.upsert_upstream(
        "default", "notion",
        ToolCatalogSnapshot(tools=[make_tool("notion", "search")]),
    )

    await store.delete_upstream("default", "github")

    snapshots = await store.load_all("default")
    assert "github" not in snapshots
    assert "notion" in snapshots


@pytest.mark.asyncio
async def test_delete_unknown_upstream_is_noop(tmp_path: Path) -> None:
    store = FileToolCatalogStore(tmp_path)
    await store.delete_upstream("default", "nonexistent")  # must not raise


@pytest.mark.asyncio
async def test_orgs_isolated(tmp_path: Path) -> None:
    store = FileToolCatalogStore(tmp_path)
    await store.upsert_upstream(
        "org_a", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "tool_a")]),
    )
    await store.upsert_upstream(
        "org_b", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "tool_b")]),
    )

    a = await store.load_all("org_a")
    b = await store.load_all("org_b")

    assert {t.original_name for t in a["github"].tools} == {"tool_a"}
    assert {t.original_name for t in b["github"].tools} == {"tool_b"}


@pytest.mark.asyncio
async def test_corrupt_file_returns_empty_and_logs(tmp_path: Path) -> None:
    """A corrupt JSON payload must not crash the registry; the
    expected behavior is to fall back to an empty snapshot so a
    fresh boot can re-discover."""
    store = FileToolCatalogStore(tmp_path)
    org_dir = tmp_path / "default"
    org_dir.mkdir(parents=True)
    (org_dir / "tool_catalog.json").write_text("{ this is not json")

    assert await store.load_all("default") == {}
