"""Phase 0 — Tool registry persistence.

Verifies that ``ToolRegistry`` writes refreshed catalogues through to a
``ToolCatalogRepository`` and re-hydrates from it on a subsequent
construction. The aim is that an admin's permissions UI keeps showing
an upstream's tool list across backend restarts even when no upstream
has reconnected.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.upstream import (
    DiscoveredPrompt,
    DiscoveredResource,
    DiscoveredResourceTemplate,
    DiscoveredTool,
)
from mcpolis.domain.ports.tool_catalog_repository import ToolCatalogSnapshot
from mcpolis.domain.services.tool_registry import ToolRegistry

from tests.unit.factories import make_upstream_definition
from tests.unit.in_memory_tool_catalog_store import (
    InMemoryToolCatalogStore,
    make_tool_catalog_store,
)


def make_tool(upstream_id: str, name: str) -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id=upstream_id,
        original_name=name,
        prefixed_name=f"{upstream_id}__{name}",
        description=None,
        input_schema={"type": "object"},
    )


def make_registry_with_store(
    upstream_ids: list[str],
    store: InMemoryToolCatalogStore,
    org_id: str = "default",
) -> tuple[ToolRegistry, MagicMock]:
    """Build a registry whose discovery is fully mocked.

    Returns the registry and the (mocked) client manager so tests can
    program ``connected_upstream_ids`` and the per-upstream session.
    """
    upstreams = [make_upstream_definition(id=uid) for uid in upstream_ids]
    client_manager = MagicMock(spec=UpstreamClientManager)
    client_manager.connected_upstream_ids = upstream_ids
    return (
        ToolRegistry(
            upstreams=upstreams,
            client_manager=client_manager,
            catalog_repo=store,
            org_id=org_id,
        ),
        client_manager,
    )


def make_session_returning(tool_names: list[str]) -> MagicMock:
    """Mock an MCP session whose ``list_tools`` returns the given names.

    Resources / templates / prompts return empty lists so a single
    helper covers the registry's discovery surface.
    """
    session = MagicMock()
    session.list_tools = AsyncMock(
        return_value=MagicMock(
            tools=[
                MagicMock(
                    name=n,
                    description=None,
                    inputSchema={"type": "object"},
                    title=None,
                    outputSchema=None,
                    annotations=None,
                    meta=None,
                )
                for n in tool_names
            ],
        ),
    )
    # MagicMock() generates the .name attribute from the kwarg; override
    # explicitly so the discovery code reads the literal string.
    for fake, name in zip(session.list_tools.return_value.tools, tool_names):
        fake.name = name
        fake.title = None
        fake.outputSchema = None
        fake.annotations = None
        fake.meta = None
    session.list_resources = AsyncMock(
        return_value=MagicMock(resources=[], nextCursor=None),
    )
    session.list_resource_templates = AsyncMock(
        return_value=MagicMock(resourceTemplates=[], nextCursor=None),
    )
    session.list_prompts = AsyncMock(
        return_value=MagicMock(prompts=[], nextCursor=None),
    )
    return session


@pytest.mark.asyncio
async def test_refresh_upstream_writes_to_store() -> None:
    store = make_tool_catalog_store()
    registry, client_manager = make_registry_with_store(["github"], store)
    session = make_session_returning(["create_issue", "list_repos"])
    client_manager.any_user_session_for_upstream.return_value = session
    client_manager.get_session.return_value = session

    await registry.refresh_upstream("github")

    snapshots = await store.load_all("default")
    assert "github" in snapshots
    assert {t.original_name for t in snapshots["github"].tools} == {
        "create_issue", "list_repos",
    }


@pytest.mark.asyncio
async def test_hydrate_loads_from_store() -> None:
    store = make_tool_catalog_store()
    seed = ToolCatalogSnapshot(
        tools=[
            make_tool("github", "create_issue"),
            make_tool("github", "list_repos"),
        ],
        resources=[
            DiscoveredResource(
                upstream_id="github",
                original_uri="github://repo",
                name="Repo",
            ),
        ],
        resource_templates=[
            DiscoveredResourceTemplate(
                upstream_id="github",
                original_uri_template="github://repo/{name}",
                name="RepoTemplate",
            ),
        ],
        prompts=[
            DiscoveredPrompt(
                upstream_id="github",
                original_name="summary",
                prefixed_name="github__summary",
            ),
        ],
    )
    await store.upsert_upstream("default", "github", seed)

    registry, _ = make_registry_with_store(["github"], store)
    await registry.hydrate()

    assert {t.original_name for t in registry.get_all_tools()} == {
        "create_issue", "list_repos",
    }
    assert len(registry.get_resources_for_upstreams(["github"])) == 1
    assert len(registry.get_resource_templates_for_upstreams(["github"])) == 1
    assert len(registry.get_prompts_for_upstreams(["github"])) == 1


@pytest.mark.asyncio
async def test_hydrate_is_idempotent() -> None:
    """Calling ``hydrate`` twice must not double-load."""
    store = make_tool_catalog_store()
    await store.upsert_upstream(
        "default", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "create_issue")]),
    )
    registry, _ = make_registry_with_store(["github"], store)

    await registry.hydrate()
    await registry.hydrate()

    assert len(registry.get_all_tools()) == 1


@pytest.mark.asyncio
async def test_hydrate_skips_unknown_upstream() -> None:
    """A persisted snapshot for an upstream that has since been
    removed from config must not surface in the registry."""
    store = make_tool_catalog_store()
    await store.upsert_upstream(
        "default", "removed",
        ToolCatalogSnapshot(tools=[make_tool("removed", "old_tool")]),
    )

    registry, _ = make_registry_with_store(["github"], store)
    await registry.hydrate()

    assert registry.get_all_tools() == []


@pytest.mark.asyncio
async def test_hydration_preserved_when_other_upstreams_refresh() -> None:
    """Refreshing upstream A must not wipe hydrated tools for upstream
    B (the catalog persists for non-connected upstreams across
    restarts)."""
    store = make_tool_catalog_store()
    await store.upsert_upstream(
        "default", "notion",
        ToolCatalogSnapshot(tools=[make_tool("notion", "search")]),
    )

    registry, client_manager = make_registry_with_store(
        ["github", "notion"], store,
    )
    # Only github is currently connected; notion is hydrated from disk.
    client_manager.connected_upstream_ids = ["github"]
    client_manager.any_user_session_for_upstream.return_value = None
    client_manager.get_session.return_value = make_session_returning(
        ["create_issue"],
    )

    await registry.hydrate()
    await registry.refresh_all()

    names = {(t.upstream_id, t.original_name) for t in registry.get_all_tools()}
    assert ("notion", "search") in names
    assert ("github", "create_issue") in names


@pytest.mark.asyncio
async def test_unregister_upstream_deletes_from_store() -> None:
    store = make_tool_catalog_store()
    await store.upsert_upstream(
        "default", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "create_issue")]),
    )
    registry, _ = make_registry_with_store(["github"], store)
    await registry.hydrate()

    await registry.unregister_upstream("github")

    snapshots = await store.load_all("default")
    assert "github" not in snapshots
    assert registry.get_all_tools() == []


@pytest.mark.asyncio
async def test_two_orgs_isolated() -> None:
    store = make_tool_catalog_store()
    await store.upsert_upstream(
        "org_a", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "tool_a")]),
    )
    await store.upsert_upstream(
        "org_b", "github",
        ToolCatalogSnapshot(tools=[make_tool("github", "tool_b")]),
    )

    registry_a, _ = make_registry_with_store(
        ["github"], store, org_id="org_a",
    )
    registry_b, _ = make_registry_with_store(
        ["github"], store, org_id="org_b",
    )
    await registry_a.hydrate()
    await registry_b.hydrate()

    assert {t.original_name for t in registry_a.get_all_tools()} == {"tool_a"}
    assert {t.original_name for t in registry_b.get_all_tools()} == {"tool_b"}


@pytest.mark.asyncio
async def test_no_repo_means_no_persistence_no_hydration() -> None:
    """A registry constructed without a catalog repo (legacy / unit-test
    path) must still work — ``hydrate`` is a no-op and refresh writes
    are silently skipped."""
    upstreams = [make_upstream_definition(id="github")]
    client_manager = MagicMock(spec=UpstreamClientManager)
    client_manager.connected_upstream_ids = ["github"]
    client_manager.any_user_session_for_upstream.return_value = None
    client_manager.get_session.return_value = make_session_returning(
        ["create_issue"],
    )

    registry = ToolRegistry(upstreams=upstreams, client_manager=client_manager)
    await registry.hydrate()  # no-op
    await registry.refresh_upstream("github")

    assert {t.original_name for t in registry.get_all_tools()} == {
        "create_issue",
    }
