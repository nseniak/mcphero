from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.upstream import DiscoveredTool, ToolAnnotations
from mcpolis.domain.services.tool_registry import (
    SEPARATOR,
    ToolRegistry,
    prefix_display_title,
)
from tests.unit.factories import make_discovered_tool, make_upstream_definition


def test_prefix_applied_to_tool_name() -> None:
    tool = make_discovered_tool(upstream_id="github", original_name="create_issue")
    assert tool.prefixed_name == f"github{SEPARATOR}create_issue"


def test_resolve_tool_returns_upstream_and_name() -> None:
    upstream = make_upstream_definition(id="github")
    # We can't call refresh_all without a real client_manager, but we can test resolve logic
    registry = ToolRegistry([upstream], None)  # type: ignore[arg-type]
    # Manually populate tools
    registry._tools = [make_discovered_tool(upstream_id="github", original_name="create_issue")]
    result = registry.resolve_tool("github__create_issue")
    assert result == ("github", "create_issue")


def test_resolve_tool_returns_none_for_unknown() -> None:
    upstream = make_upstream_definition(id="github")
    registry = ToolRegistry([upstream], None)  # type: ignore[arg-type]
    assert registry.resolve_tool("unknown__tool") is None
    assert registry.resolve_tool("no_separator") is None


def test_get_tools_for_upstreams_filters() -> None:
    upstream_gh = make_upstream_definition(id="github")
    upstream_sl = make_upstream_definition(id="slack")
    registry = ToolRegistry([upstream_gh, upstream_sl], None)  # type: ignore[arg-type]
    registry._tools = [
        make_discovered_tool(upstream_id="github", original_name="create_issue"),
        make_discovered_tool(upstream_id="slack", original_name="send_message"),
    ]
    filtered = registry.get_tools_for_upstreams(["github"])
    assert len(filtered) == 1
    assert filtered[0].upstream_id == "github"


def test_to_mcp_tools_converts() -> None:
    upstream = make_upstream_definition(id="github")
    registry = ToolRegistry([upstream], None)  # type: ignore[arg-type]
    discovered = [make_discovered_tool(upstream_id="github", original_name="create_issue")]
    mcp_tools = registry.to_mcp_tools(discovered)
    assert len(mcp_tools) == 1
    assert mcp_tools[0].name == "github__create_issue"
    assert mcp_tools[0].description == "Does a thing"


# ── display-name title prefixing ─────────────────────────────────────
#
# Same-named tools from different upstreams ("Get Projects" on both
# mixpanel and mixpanel2) only differ by their namespaced ``name``. The
# human ``title`` is folded with the upstream display name so a person
# (and the approval dialog) can tell them apart. Clients display
# ``title`` if present else ``annotations.title``, so both must carry
# the prefix.


def _make_titled_tool(
    *,
    upstream_id: str,
    title: str | None,
    annotations_title: str | None = None,
) -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id=upstream_id,
        original_name="Get-Projects",
        prefixed_name=f"{upstream_id}{SEPARATOR}Get-Projects",
        description="d",
        input_schema={"type": "object", "properties": {}},
        title=title,
        annotations=(
            ToolAnnotations(title=annotations_title)
            if annotations_title is not None else None
        ),
    )


def test_prefix_display_title_passes_through_when_no_title() -> None:
    assert prefix_display_title("Mixpanel", None) is None
    assert prefix_display_title("Mixpanel", "") == ""
    assert prefix_display_title("Mixpanel", "Get Projects") == "Mixpanel: Get Projects"


def test_to_mcp_tools_prefixes_title_with_display_name() -> None:
    upstream = make_upstream_definition(id="mixpanel", display_name="Mixpanel")
    registry = ToolRegistry([upstream], None)  # type: ignore[arg-type]
    discovered = [_make_titled_tool(upstream_id="mixpanel", title="Get Projects")]
    mcp_tools = registry.to_mcp_tools(discovered)
    assert mcp_tools[0].name == "mixpanel__Get-Projects"
    assert mcp_tools[0].title == "Mixpanel: Get Projects"


def test_to_mcp_tools_prefixes_annotations_title() -> None:
    upstream = make_upstream_definition(
        id="everything", display_name="Everything",
    )
    registry = ToolRegistry([upstream], None)  # type: ignore[arg-type]
    # Older-style server: only ``annotations.title`` is set.
    discovered = [
        _make_titled_tool(
            upstream_id="everything",
            title=None,
            annotations_title="Get Annotated Message",
        )
    ]
    mcp_tools = registry.to_mcp_tools(discovered)
    tool = mcp_tools[0]
    assert tool.title is None
    assert tool.annotations is not None
    assert tool.annotations.title == "Everything: Get Annotated Message"


def test_to_mcp_tools_leaves_titleless_tool_untitled() -> None:
    # No title in either field: the namespaced ``name`` already carries
    # the upstream, so we don't synthesize a title (which would hide the
    # real callable name).
    upstream = make_upstream_definition(id="mixpanel", display_name="Mixpanel")
    registry = ToolRegistry([upstream], None)  # type: ignore[arg-type]
    discovered = [
        make_discovered_tool(
            upstream_id="mixpanel", original_name="List-Experiments",
        )
    ]
    mcp_tools = registry.to_mcp_tools(discovered)
    assert mcp_tools[0].title is None
    assert mcp_tools[0].annotations is None


def test_display_name_for_falls_back_to_id() -> None:
    upstream = make_upstream_definition(id="mixpanel", display_name="")
    registry = ToolRegistry([upstream], None)  # type: ignore[arg-type]
    assert registry.display_name_for("mixpanel") == "mixpanel"
    assert registry.display_name_for("unknown") == "unknown"


# ── _discover_upstream session selection ─────────────────────────────
#
# ``_discover_upstream`` tries the admin per-user session first and
# falls back to the shared session. This ordering is load-bearing:
#
#   * For OAuth upstreams, the shared session is an unauthenticated
#     discovery-connect that can only list tools the server exposes
#     anonymously — usually zero. The admin's authenticated per-user
#     session sees the full toolset.
#   * For service_account upstreams, there's no admin session; the
#     fallback is the only path.
#
# A regression that inverts the order (or drops the fallback) silently
# hands prod an empty tool list for OAuth upstreams.


def _make_discovery_session(tool_name: str) -> MagicMock:
    session = MagicMock()
    session.list_tools = AsyncMock(
        return_value=mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name=tool_name,
                    description=f"tool from {tool_name} session",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )
    )
    return session


@pytest.mark.asyncio
async def test_discover_upstream_prefers_per_user_session() -> None:
    """When both a per-user session (the post-Phase-2 home of
    admin_oauth's slot owner) and a service-account shared session
    exist for an upstream, ``_discover_upstream`` lists tools from the
    per-user session — that's the one with the authenticated identity.
    """
    upstream = make_upstream_definition(id="notion")
    client_manager = UpstreamClientManager([upstream])
    user_session = _make_discovery_session("admin_tool")
    shared_session = _make_discovery_session("shared_tool")
    from tests.unit._state_seed import seed_shared_session, seed_user_session
    seed_user_session(client_manager, "notion", "admin@co.com", session=user_session)
    seed_shared_session(client_manager, "notion", session=shared_session)

    registry = ToolRegistry([upstream], client_manager)
    tools = await registry._discover_upstream("notion")  # pyright: ignore[reportPrivateUsage]

    user_session.list_tools.assert_awaited_once()
    shared_session.list_tools.assert_not_awaited()
    assert [t.original_name for t in tools] == ["admin_tool"]


@pytest.mark.asyncio
async def test_discover_upstream_falls_back_to_shared_when_no_admin() -> None:
    """No admin session → fall back to the shared session. This is the
    service_account path and the pre-OAuth discovery-connect path."""
    upstream = make_upstream_definition(id="github")
    client_manager = UpstreamClientManager([upstream])
    shared_session = _make_discovery_session("shared_tool")
    from tests.unit._state_seed import seed_shared_session
    seed_shared_session(client_manager, "github", session=shared_session)

    registry = ToolRegistry([upstream], client_manager)
    tools = await registry._discover_upstream("github")  # pyright: ignore[reportPrivateUsage]

    shared_session.list_tools.assert_awaited_once()
    assert [t.original_name for t in tools] == ["shared_tool"]


@pytest.mark.asyncio
async def test_discover_upstream_raises_when_neither_session_exists() -> None:
    """If neither an admin nor a shared session is available, the
    KeyError from the second lookup must propagate so ``refresh_all``
    can catch it and drop the upstream's tools from the cache."""
    upstream = make_upstream_definition(id="slack")
    client_manager = UpstreamClientManager([upstream])
    # No sessions seeded.

    registry = ToolRegistry([upstream], client_manager)
    with pytest.raises(KeyError):
        await registry._discover_upstream("slack")  # pyright: ignore[reportPrivateUsage]
