"""Smoke tests for the bundled demo MCP server.

The demo lives at ``backend/src/mcpolis/dev/demo_mcp_server.py`` and is
auto-mounted by the backend at ``/dev/mcp-demo`` when
``MCPOLIS_DEMO_MOUNT=1``. These tests pin the surface (tool /
resource / prompt counts), the widget URI invariants the gateway's
Part A rewrite layer depends on, and the on-disk presence of every
widget JS file the shell HTML imports.
"""
from __future__ import annotations

import pytest

from mcpolis.dev.demo_mcp_server import (
    DEMO_INSTRUCTIONS,
    DEMO_UPSTREAM_ID,
    WIDGET_REGISTRY,
    WIDGETS_DIR,
    build_demo_app,
    build_demo_mcp,
)


def test_widget_registry_files_exist_on_disk() -> None:
    """Every widget name we register a tool for must have its JS file
    present — a typo would silently 404 the dynamic import inside the
    iframe and the widget would render blank."""
    for _, (_, filename) in WIDGET_REGISTRY.items():
        assert (WIDGETS_DIR / filename).is_file(), (
            f"missing widget JS file: {filename}"
        )


@pytest.mark.asyncio
async def test_demo_mcp_exposes_all_planned_tools() -> None:
    mcp = build_demo_mcp("http://localhost:8080")
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        # Preserved e2e fixture tools.
        "echo", "add", "greet",
        # Five widget-opening tools.
        "open_inline_widget", "open_fullscreen_widget", "open_pip_widget",
        "open_counter_widget", "open_solar_system_widget",
        # Solar-system callback tools the widget invokes.
        "record_planet_click", "get_last_clicked_planet",
        "submit_explanation",
    }
    assert expected.issubset(names), (
        f"missing tools: {expected - names}"
    )


@pytest.mark.asyncio
async def test_widget_tools_carry_widget_uri_meta_for_registered_widgets() -> None:
    """Every widget tool's ``_meta.ui.resourceUri`` must point at a
    ``ui://`` resource we actually register. The Part A rewrite layer
    rewraps these URIs into ``mcphero://...`` for clients; a typo
    here would cause ``resources/read`` to fail with ``Unknown
    resource`` after rewrite."""
    mcp = build_demo_mcp("http://localhost:8080")
    tools = await mcp.list_tools()

    # Map widget URI → expected widget name from the registry.
    uri_to_name = {uri: name for name, (uri, _) in WIDGET_REGISTRY.items()}

    widget_tools = [
        t for t in tools if t.name.startswith("open_") and t.meta is not None
    ]
    assert len(widget_tools) == 5

    for tool in widget_tools:
        assert tool.meta is not None
        nested = tool.meta.get("ui")
        assert isinstance(nested, dict), tool.name
        nested_uri = nested.get("resourceUri")
        assert isinstance(nested_uri, str), tool.name
        flat_uri = tool.meta.get("ui/resourceUri")
        assert flat_uri == nested_uri, (
            f"both meta keys must agree: {tool.name}"
        )
        # Every advertised widget URI must map to a widget we have
        # registered (registry-based assertion catches drift between
        # the tool decorator and the widget JS file mapping).
        assert nested_uri in uri_to_name, tool.name


@pytest.mark.asyncio
async def test_demo_mcp_exposes_static_and_template_resources() -> None:
    mcp = build_demo_mcp("http://localhost:8080")
    resources = await mcp.list_resources()
    templates = await mcp.list_resource_templates()
    resource_uris = {str(r.uri) for r in resources}
    template_uri_templates = {t.uriTemplate for t in templates}

    # Static + 5 widget shells.
    assert "test://hello-world" in resource_uris
    for name, (uri, _) in WIDGET_REGISTRY.items():
        assert uri in resource_uris, (
            f"missing widget resource for {name}: {uri}"
        )
    # One templated resource (used by 13-resources-and-prompts and the
    # new e2e demo widget spec).
    assert "test://greeting/{name}" in template_uri_templates


@pytest.mark.asyncio
async def test_demo_mcp_exposes_both_planned_prompts() -> None:
    mcp = build_demo_mcp("http://localhost:8080")
    prompts = await mcp.list_prompts()
    names = {p.name for p in prompts}
    assert {"greet_prompt", "summarize_clicks_prompt"}.issubset(names)


def test_demo_mcp_advertises_ui_extension_in_capabilities() -> None:
    """The MCP-Apps marker must round-trip through ``initialize`` so
    the gateway's Part A aggregation has something to forward."""
    mcp = build_demo_mcp("http://localhost:8080")
    server = mcp._mcp_server  # pyright: ignore[reportPrivateUsage]
    init_opts = server.create_initialization_options()
    caps = init_opts.capabilities.model_dump(by_alias=True, exclude_none=True)
    extensions = caps.get("extensions")
    assert extensions is not None
    assert "io.modelcontextprotocol/ui" in extensions
    assert "text/html;profile=mcp-app" in (
        extensions["io.modelcontextprotocol/ui"]["mimeTypes"]
    )


def test_demo_instructions_match_plan_text() -> None:
    """Pin the instructions text — ``test_e2e/13-resources-and-prompts``
    asserts on a substring of this string; if it changes, that test
    must be updated in lockstep."""
    assert "Demo upstream for the MCP Hero test suite" in DEMO_INSTRUCTIONS


def test_build_demo_app_constructs_starlette_routes() -> None:
    """Sanity: the wiring at ``/dev/mcp-demo`` exposes the four routes
    the widgets and the gateway depend on (MCP, widget JS, two WS)."""
    app, _mcp = build_demo_app("http://localhost:8080")
    paths: list[str] = []
    for r in app.router.routes:
        path = getattr(r, "path", None)
        if isinstance(path, str):
            paths.append(path)
    # The FastMCP-managed Starlette app already owns ``/mcp``; we
    # appended the dev routes alongside it.
    assert "/mcp" in paths
    assert "/widget/{name}.js" in paths
    assert "/ws/counter" in paths
    assert "/ws/explanations" in paths


def test_demo_upstream_id_constant_matches_settings_default() -> None:
    """Pin the constant so changing it requires touching this test —
    it's used both inside the demo (FastMCP server name) and as the
    default upstream id the backend seeds on startup."""
    assert DEMO_UPSTREAM_ID == "mcp-demo"


async def _all_widget_tool_meta_uris(public_url: str) -> list[str]:
    mcp = build_demo_mcp(public_url)
    tools = await mcp.list_tools()
    out: list[str] = []
    for t in tools:
        if t.meta is None:
            continue
        nested = t.meta.get("ui")
        if isinstance(nested, dict):
            uri = nested.get("resourceUri")
            if isinstance(uri, str):
                out.append(uri)
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("public_url", ["http://localhost:8080", "https://dev.example.com"])
async def test_widget_tool_meta_uris_independent_of_public_url(public_url: str) -> None:
    """The widget URI is the *MCP* URI (``ui://...``) — it must NOT
    bake in ``public_url``. Only the resource shell HTML and CSP
    domains depend on ``public_url``."""
    uris = await _all_widget_tool_meta_uris(public_url)
    for uri in uris:
        assert uri.startswith("ui://"), uri
