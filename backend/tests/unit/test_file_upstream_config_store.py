"""Tests for FileUpstreamConfigStore (mcp.json + config.json)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_upstream_config_store import FileUpstreamConfigStore
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit.factories import make_upstream_definition


def make_store(tmp_path: Path) -> FileUpstreamConfigStore:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"upstreams": {}, "roles": {}, "users": {}}))
    mcp_store = McpJsonStore(mcp_json)
    config_store = FileConfigStore(config_path)
    return FileUpstreamConfigStore(mcp_store, config_store)


@pytest.mark.asyncio
async def test_add_and_get(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    upstream = make_upstream_definition(id="github", display_name="GitHub")
    await store.add(DEFAULT_ORG_ID,upstream)

    loaded = await store.get(DEFAULT_ORG_ID,"github")
    assert loaded is not None
    assert loaded.id == "github"
    assert loaded.display_name == "GitHub"


@pytest.mark.asyncio
async def test_add_writes_to_mcp_json(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    from mcpolis.domain.model.upstream import TransportType
    upstream = make_upstream_definition(
        id="github", url="https://mcp.github.com/sse",
        transport=TransportType.streamable_http,
    )
    await store.add(DEFAULT_ORG_ID,upstream)

    # Verify mcp.json has the connection
    data = json.loads((tmp_path / "mcp.json").read_text())
    assert "github" in data["mcpServers"]
    assert data["mcpServers"]["github"]["url"] == "https://mcp.github.com/sse"


@pytest.mark.asyncio
async def test_add_writes_options_to_config(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    upstream = make_upstream_definition(
        id="github", display_name="GitHub MCP"
    )
    await store.add(DEFAULT_ORG_ID,upstream)

    # Verify config.json has the options
    data = json.loads((tmp_path / "config.json").read_text())
    assert "github" in data["upstreams"]
    assert data["upstreams"]["github"]["display_name"] == "GitHub MCP"


@pytest.mark.asyncio
async def test_add_duplicate_raises(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.add(DEFAULT_ORG_ID,make_upstream_definition(id="github"))
    with pytest.raises(ValueError, match="already exists"):
        await store.add(DEFAULT_ORG_ID,make_upstream_definition(id="github"))


@pytest.mark.asyncio
async def test_update(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.add(DEFAULT_ORG_ID,make_upstream_definition(id="github", display_name="Old"))
    updated = make_upstream_definition(id="github", display_name="New")
    await store.update(DEFAULT_ORG_ID,updated)

    loaded = await store.get(DEFAULT_ORG_ID,"github")
    assert loaded is not None
    assert loaded.display_name == "New"


@pytest.mark.asyncio
async def test_update_nonexistent_raises(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        await store.update(DEFAULT_ORG_ID,make_upstream_definition(id="missing"))


@pytest.mark.asyncio
async def test_remove(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.add(DEFAULT_ORG_ID,make_upstream_definition(id="github"))
    await store.remove(DEFAULT_ORG_ID,"github")

    assert await store.get(DEFAULT_ORG_ID,"github") is None
    assert await store.get_all(DEFAULT_ORG_ID) == []

    # Verify removed from mcp.json
    data = json.loads((tmp_path / "mcp.json").read_text())
    assert "github" not in data["mcpServers"]


@pytest.mark.asyncio
async def test_remove_nonexistent_raises(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        await store.remove(DEFAULT_ORG_ID,"missing")


@pytest.mark.asyncio
async def test_get_all(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.add(DEFAULT_ORG_ID,make_upstream_definition(id="github"))
    await store.add(DEFAULT_ORG_ID,make_upstream_definition(id="slack"))
    all_upstreams = await store.get_all(DEFAULT_ORG_ID)
    ids = {u.id for u in all_upstreams}
    assert ids == {"github", "slack"}
