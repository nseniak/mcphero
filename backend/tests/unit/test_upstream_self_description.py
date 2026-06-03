"""Tests for capturing the upstream's ``initialize`` self-description.

When the gateway opens a session to an upstream MCP server, the
upstream's ``InitializeResult`` carries free-form text the gateway
should preserve and re-advertise:

* ``instructions`` (top-level on ``InitializeResult``)
* ``serverInfo.name`` / ``version`` / ``websiteUrl``
* ``serverInfo.description`` — Notion fills this in empirically;
  ``Implementation`` is ``extra="allow"`` so the SDK preserves it.

These tests pin that the adapter task captures the result, the client
manager exposes it via ``get_self_description``, and ``None`` flows
through cleanly when the upstream advertises nothing.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.adapters.upstream_clients.http_adapter import HttpConnectionTask
from mcpolis.domain.model.upstream import (
    TransportType,
    UpstreamSelfDescription,
)
from tests.unit.factories import make_upstream_definition


# --- Adapter-level capture ---------------------------------------------------


def make_initialize_result(
    *,
    name: str = "notion-mcp",
    version: str = "0.4.2",
    instructions: str | None = "Use the Notion MCP to search and read pages.",
    description: str | None = "MCP server for Notion's API.",
    website_url: str | None = "https://example.invalid/notion-mcp",
) -> mcp_types.InitializeResult:
    """Build an ``InitializeResult`` shaped like a real upstream's response.

    ``description`` is set as an extra attribute on ``Implementation``
    (the type allows extras), matching what the SDK does when an upstream
    sends a non-spec ``serverInfo.description`` field.
    """
    server_info_kwargs: dict[str, Any] = {
        "name": name,
        "version": version,
    }
    if website_url is not None:
        server_info_kwargs["websiteUrl"] = website_url
    if description is not None:
        # ``Implementation`` has ``model_config = ConfigDict(extra="allow")``
        server_info_kwargs["description"] = description
    return mcp_types.InitializeResult(
        protocolVersion="2024-11-05",
        capabilities=mcp_types.ServerCapabilities(),
        serverInfo=mcp_types.Implementation(**server_info_kwargs),
        instructions=instructions,
    )


def _build_self_description_from_init(
    init_result: mcp_types.InitializeResult,
) -> UpstreamSelfDescription:
    """Mirror what the adapter's ``_run`` does — exercise the construction
    branch in isolation without spinning up a real subprocess / HTTP
    server. Adapter integration is covered by the dedicated
    ``test_mcp_integration`` suite; here we just pin the field mapping.
    """
    si = init_result.serverInfo
    return UpstreamSelfDescription(
        name=si.name,
        version=si.version,
        instructions=init_result.instructions,
        description=getattr(si, "description", None),
        website_url=si.websiteUrl,
    )


def test_self_description_preserves_instructions_and_description() -> None:
    init_result = make_initialize_result(
        instructions="hello",
        description="MCP server for Notion's API.",
    )
    sd = _build_self_description_from_init(init_result)
    assert sd.instructions == "hello"
    assert sd.description == "MCP server for Notion's API."
    assert sd.name == "notion-mcp"
    assert sd.version == "0.4.2"
    assert sd.website_url == "https://example.invalid/notion-mcp"


def test_self_description_handles_missing_instructions_and_description() -> None:
    init_result = make_initialize_result(
        instructions=None, description=None, website_url=None,
    )
    sd = _build_self_description_from_init(init_result)
    assert sd.instructions is None
    assert sd.description is None
    assert sd.website_url is None
    # name/version are required on Implementation, so they always survive.
    assert sd.name == "notion-mcp"
    assert sd.version == "0.4.2"


@pytest.mark.asyncio
async def test_http_adapter_self_description_field_starts_none() -> None:
    """A freshly-constructed ``HttpConnectionTask`` exposes
    ``self_description`` as ``None`` until ``_run`` populates it. Tests
    here drive the construction without spinning up a real session — the
    integration test exercises the actual capture path."""
    upstream = make_upstream_definition(
        id="notion",
        transport=TransportType.streamable_http,
        url="http://example.invalid/mcp",
    )
    task = HttpConnectionTask(upstream, user_id="__shared__")
    assert task.self_description is None


# --- ClientManager wiring ---------------------------------------------------


def make_manager_with_seeded_task(
    upstream_id: str = "notion",
    *,
    self_description: UpstreamSelfDescription | None = None,
) -> tuple[UpstreamClientManager, MagicMock]:
    """Seed a stub ``ConnectionTask`` so we can drive the
    ``connect_shared`` recording path without a real session."""
    upstream = make_upstream_definition(
        id=upstream_id,
        transport=TransportType.streamable_http,
        url="http://upstream.invalid/mcp",
    )
    mgr = UpstreamClientManager([upstream])

    from tests.unit._state_seed import seed_shared_session
    task = MagicMock(name="ConnectionTask")
    task.close = AsyncMock()
    task.server_info = None
    task.self_description = self_description
    seed_shared_session(
        mgr, upstream_id,
        session=MagicMock(),
        task=task,
        self_description=self_description,
    )
    return mgr, task


def test_get_self_description_returns_seeded_value() -> None:
    sd = UpstreamSelfDescription(
        name="notion-mcp",
        version="0.4.2",
        instructions="Use it well.",
        description="MCP server for Notion.",
        website_url="https://example.invalid/notion",
    )
    mgr, _ = make_manager_with_seeded_task("notion", self_description=sd)
    got = mgr.get_self_description("notion")
    assert got is sd


def test_get_self_description_returns_none_when_missing() -> None:
    mgr, _ = make_manager_with_seeded_task("notion")
    assert mgr.get_self_description("notion") is None
    assert mgr.get_self_description("does-not-exist") is None


@pytest.mark.asyncio
async def test_close_shared_clears_self_description() -> None:
    """``disconnect_upstream`` must remove the recorded self-description
    so a later reconnect doesn't surface stale text."""
    sd = UpstreamSelfDescription(name="notion-mcp", version="0.4.2")
    mgr, _ = make_manager_with_seeded_task("notion", self_description=sd)
    assert mgr.get_self_description("notion") is sd
    await mgr.disconnect_upstream("notion")
    assert mgr.get_self_description("notion") is None
