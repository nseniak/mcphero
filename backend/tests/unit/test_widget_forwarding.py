"""Part A — widget forwarding tests.

Covers four surfaces:

1. ``_meta`` round-trips through ``Discovered*`` records and the wire
   ``Tool`` / ``Resource`` / ``Prompt`` shapes (modulo URI rewrite).
2. Tool widget URIs (``_meta.ui.resourceUri`` and the legacy flat
   ``_meta["ui/resourceUri"]``) are rewrapped via ``wrap_resource_uri``
   so that the gateway's ``resources/read`` round-trip lands back on
   the original ``ui://`` URI on the upstream session.
3. ``capabilities.extensions`` advertised by upstreams is aggregated
   into the gateway's ``initialize`` capabilities.
4. ``ReadResourceContents`` carries ``_meta`` end-to-end (CSP hints
   need to survive ``resources/read``).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest
from pydantic import AnyUrl

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.upstream import (
    DiscoveredPrompt,
    DiscoveredResource,
    DiscoveredResourceTemplate,
    DiscoveredTool,
    UpstreamSelfDescription,
)
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.uri_wrapping import (
    unwrap_resource_uri,
    wrap_resource_uri,
)
from mcpolis.entrypoints.controllers.gateway_controller import (
    _aggregated_extensions,
    _apply_widget_meta_rewrites,
    _build_prompt,
    _build_resource,
    _build_resource_template,
    _resolve_bare_resource_uri,
    _resolve_bare_tool_name,
    _result_contents_to_iterable,
    _rewrite_widget_meta,
)
from tests.unit.factories import make_upstream_definition

WIDGET_URI = "ui://demo/widget/inline"


# ─── Discovered model meta round-trip ─────────────────────────────────


def test_discovered_tool_round_trips_meta() -> None:
    t = DiscoveredTool(
        upstream_id="demo", original_name="open_widget",
        prefixed_name="demo__open_widget", description="d",
        input_schema={"type": "object"},
        meta={"ui": {"resourceUri": WIDGET_URI}},
    )
    assert t.meta == {"ui": {"resourceUri": WIDGET_URI}}


def test_discovered_resource_round_trips_meta() -> None:
    r = DiscoveredResource(
        upstream_id="demo", original_uri=WIDGET_URI, name="w",
        meta={"ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}},
    )
    assert r.meta is not None
    assert r.meta["ui"]["csp"]["resourceDomains"] == ["https://unpkg.com"]


def test_discovered_resource_template_round_trips_meta() -> None:
    t = DiscoveredResourceTemplate(
        upstream_id="demo", original_uri_template="ui://demo/things/{id}",
        name="thing", meta={"foo": "bar"},
    )
    assert t.meta == {"foo": "bar"}


def test_discovered_prompt_round_trips_meta() -> None:
    p = DiscoveredPrompt(
        upstream_id="demo", original_name="greet",
        prefixed_name="demo__greet", meta={"hint": "x"},
    )
    assert p.meta == {"hint": "x"}


# ─── ToolRegistry: discovery + emit ───────────────────────────────────


def _make_tool_session(tools: list[mcp_types.Tool]) -> Any:
    session = MagicMock()
    session.list_tools = AsyncMock(
        return_value=mcp_types.ListToolsResult(tools=tools),
    )
    return session


@pytest.mark.asyncio
async def test_discover_upstream_captures_meta() -> None:
    """``_discover_upstream`` must capture upstream ``_meta`` so the
    widget URI rewrite layer downstream has something to work with.
    """
    tool = mcp_types.Tool.model_validate({
        "name": "open_widget",
        "description": "d",
        "inputSchema": {"type": "object"},
        "_meta": {
            "ui": {"resourceUri": WIDGET_URI},
            "ui/resourceUri": WIDGET_URI,
        },
    })
    upstream = make_upstream_definition(id="demo")
    cm = UpstreamClientManager([upstream])
    from tests.unit._state_seed import seed_shared_session
    seed_shared_session(cm, "demo", session=_make_tool_session([tool]))

    registry = ToolRegistry([upstream], cm)
    discovered = await registry._discover_upstream("demo")  # pyright: ignore[reportPrivateUsage]
    assert len(discovered) == 1
    assert discovered[0].meta == {
        "ui": {"resourceUri": WIDGET_URI},
        "ui/resourceUri": WIDGET_URI,
    }


def test_to_mcp_tools_emits_meta_via_alias() -> None:
    """Constructing ``Tool(meta=...)`` silently drops it (alias trap,
    FINDINGS §3). ``to_mcp_tools`` must use ``model_validate``."""
    upstream = make_upstream_definition(id="demo")
    registry = ToolRegistry([upstream], MagicMock())
    tools = registry.to_mcp_tools([
        DiscoveredTool(
            upstream_id="demo", original_name="open_widget",
            prefixed_name="demo__open_widget",
            description="d", input_schema={"type": "object"},
            meta={"ui": {"resourceUri": WIDGET_URI}},
        ),
    ])
    assert len(tools) == 1
    dumped = tools[0].model_dump(by_alias=True, exclude_none=True)
    assert dumped["_meta"] == {"ui": {"resourceUri": WIDGET_URI}}


def test_to_mcp_tools_omits_meta_key_when_absent() -> None:
    """Tools without ``_meta`` must not emit ``"_meta": null`` —
    that clutters the wire and confuses some clients."""
    upstream = make_upstream_definition(id="demo")
    registry = ToolRegistry([upstream], MagicMock())
    tools = registry.to_mcp_tools([
        DiscoveredTool(
            upstream_id="demo", original_name="echo",
            prefixed_name="demo__echo",
            description="echo", input_schema={"type": "object"},
        ),
    ])
    dumped = tools[0].model_dump(by_alias=True, exclude_none=True)
    assert "_meta" not in dumped


# ─── Widget URI rewrite ───────────────────────────────────────────────


def test_rewrite_widget_meta_rewrites_nested_ui_resourceuri() -> None:
    rewritten = _rewrite_widget_meta(
        {"ui": {"resourceUri": WIDGET_URI}},
        org_slug="acme", upstream_id="demo",
    )
    assert rewritten is not None
    new_uri = rewritten["ui"]["resourceUri"]
    # Widget URIs MUST keep the ``ui://`` scheme so the MCP Apps
    # client validation passes (Inspector / Claude reject any other
    # scheme with "Invalid UI resource URI").
    assert new_uri.startswith("ui://mcphero/orgs/acme/upstreams/demo/widgets/")
    decoded = unwrap_resource_uri(new_uri)
    assert decoded.original_uri == WIDGET_URI
    assert decoded.upstream_id == "demo"
    assert decoded.org_slug == "acme"


def test_rewrite_widget_meta_rewrites_legacy_flat_key() -> None:
    rewritten = _rewrite_widget_meta(
        {"ui/resourceUri": WIDGET_URI},
        org_slug="acme", upstream_id="demo",
    )
    assert rewritten is not None
    decoded = unwrap_resource_uri(rewritten["ui/resourceUri"])
    assert decoded.original_uri == WIDGET_URI


def test_rewrite_widget_meta_rewrites_both_keys_idempotently() -> None:
    """ext-apps SDK emits both shapes — both must rewrite to the same
    wrapped URI so a client honoring either gets a consistent target."""
    rewritten = _rewrite_widget_meta(
        {
            "ui": {"resourceUri": WIDGET_URI},
            "ui/resourceUri": WIDGET_URI,
        },
        org_slug="acme", upstream_id="demo",
    )
    assert rewritten is not None
    assert rewritten["ui"]["resourceUri"] == rewritten["ui/resourceUri"]


def test_rewrite_widget_meta_passes_unrelated_keys_through() -> None:
    rewritten = _rewrite_widget_meta(
        {"foo": "bar", "ui": {"csp": {"resourceDomains": ["https://x"]}}},
        org_slug="acme", upstream_id="demo",
    )
    assert rewritten is not None
    assert rewritten["foo"] == "bar"
    # ``ui.csp`` is left alone — only ``ui.resourceUri`` is rewritten.
    assert rewritten["ui"]["csp"]["resourceDomains"] == ["https://x"]


def test_rewrite_widget_meta_returns_none_for_none() -> None:
    assert _rewrite_widget_meta(None, org_slug="acme", upstream_id="demo") is None


def test_rewrite_widget_meta_preserves_ui_scheme_prefix() -> None:
    """MCP Apps clients hard-validate the ``ui://`` prefix on widget
    URIs; a rewrite that drops it (e.g. to ``mcphero://``) makes
    the Inspector / Claude reject the tool with "Invalid UI resource
    URI". This pins the prefix so a future refactor can't regress
    that contract silently."""
    rewritten_nested = _rewrite_widget_meta(
        {"ui": {"resourceUri": WIDGET_URI}},
        org_slug="acme", upstream_id="demo",
    )
    rewritten_flat = _rewrite_widget_meta(
        {"ui/resourceUri": WIDGET_URI},
        org_slug="acme", upstream_id="demo",
    )
    assert rewritten_nested is not None
    assert rewritten_flat is not None
    assert rewritten_nested["ui"]["resourceUri"].startswith("ui://")
    assert rewritten_flat["ui/resourceUri"].startswith("ui://")


def test_rewrite_widget_meta_does_not_mutate_input() -> None:
    original: dict[str, Any] = {"ui": {"resourceUri": WIDGET_URI}}
    _rewrite_widget_meta(original, org_slug="acme", upstream_id="demo")
    # Caller's dict must be unchanged.
    assert original == {"ui": {"resourceUri": WIDGET_URI}}


def test_apply_widget_meta_rewrites_pairs_by_index() -> None:
    upstream = make_upstream_definition(id="demo")
    registry = ToolRegistry([upstream], MagicMock())
    discovered = [
        DiscoveredTool(
            upstream_id="demo", original_name="open_widget",
            prefixed_name="demo__open_widget", description="d",
            input_schema={"type": "object"},
            meta={"ui": {"resourceUri": WIDGET_URI}},
        ),
        DiscoveredTool(
            upstream_id="demo", original_name="echo",
            prefixed_name="demo__echo", description="echo",
            input_schema={"type": "object"},
        ),
    ]
    tools = registry.to_mcp_tools(discovered)
    _apply_widget_meta_rewrites(tools, discovered, org_slug="acme")
    rewritten = tools[0].meta
    assert rewritten is not None
    assert rewritten["ui"]["resourceUri"].startswith(
        "ui://mcphero/orgs/acme/upstreams/demo/widgets/",
    )
    # Tool without meta stays untouched.
    assert tools[1].meta is None


# ─── URI wrapping handles ui:// scheme cleanly ────────────────────────


def test_wrap_resource_uri_round_trips_ui_scheme() -> None:
    wrapped = wrap_resource_uri(
        org_slug="acme", upstream_id="demo",
        original_uri="ui://demo/widget/inline",
    )
    decoded = unwrap_resource_uri(wrapped)
    assert decoded.original_uri == "ui://demo/widget/inline"
    assert decoded.is_template is False


def test_wrap_resource_uri_round_trips_ui_template() -> None:
    wrapped = wrap_resource_uri(
        org_slug="acme", upstream_id="demo",
        original_uri="ui://demo/things/{id}",
        is_template=True,
    )
    decoded = unwrap_resource_uri(wrapped)
    assert decoded.original_uri == "ui://demo/things/{id}"
    assert decoded.is_template is True


# ─── Resource / template / prompt builders forward meta ───────────────


def test_build_resource_forwards_meta() -> None:
    discovered = DiscoveredResource(
        upstream_id="demo",
        original_uri="ui://demo/widget/inline",
        name="inline-widget",
        mime_type="text/html;profile=mcp-app",
        meta={"ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}},
    )
    resource = _build_resource(
        discovered=discovered, org_slug="acme", name_prefix="demo__",
        display_name="Demo",
    )
    dumped = resource.model_dump(by_alias=True, exclude_none=True)
    assert dumped["_meta"] == {
        "ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}},
    }


def test_build_resource_template_forwards_meta() -> None:
    discovered = DiscoveredResourceTemplate(
        upstream_id="demo", original_uri_template="ui://demo/things/{id}",
        name="thing", meta={"foo": "bar"},
    )
    tpl = _build_resource_template(
        discovered=discovered, org_slug="acme", name_prefix="demo__",
        display_name="Demo",
    )
    dumped = tpl.model_dump(by_alias=True, exclude_none=True)
    assert dumped["_meta"] == {"foo": "bar"}


def test_build_prompt_forwards_meta() -> None:
    discovered = DiscoveredPrompt(
        upstream_id="demo", original_name="greet",
        prefixed_name="demo__greet", meta={"hint": "x"},
    )
    prompt = _build_prompt(
        discovered=discovered, name_prefix="demo__", display_name="Demo",
    )
    dumped = prompt.model_dump(by_alias=True, exclude_none=True)
    assert dumped["_meta"] == {"hint": "x"}


# ─── _result_contents_to_iterable preserves meta + mime exactness ─────


def test_read_resource_preserves_meta_and_exact_mime() -> None:
    """``text/html;profile=mcp-app`` must round-trip byte-for-byte
    (FINDINGS §1: "Resource is served with MIME exactly
    text/html;profile=mcp-app"); ``_meta`` (CSP hints) must
    survive through ``_result_contents_to_iterable``."""
    result = mcp_types.ReadResourceResult(
        contents=[
            mcp_types.TextResourceContents.model_validate({
                "uri": AnyUrl("ui://demo/widget/inline"),
                "mimeType": "text/html;profile=mcp-app",
                "text": "<html>...</html>",
                "_meta": {"ui": {"csp": {"resourceDomains": ["https://x"]}}},
            }),
        ],
    )
    items = list(_result_contents_to_iterable(result))
    assert len(items) == 1
    assert items[0].mime_type == "text/html;profile=mcp-app"
    assert items[0].meta == {
        "ui": {"csp": {"resourceDomains": ["https://x"]}},
    }


# ─── _aggregated_extensions ──────────────────────────────────────────


def _make_runtime_with_extensions(
    extensions_by_upstream: dict[str, dict[str, dict[str, Any]]],
) -> Any:
    """Build a fake runtime whose ``tool_registry`` advertises the
    given extension dict per upstream."""
    runtime = MagicMock()
    runtime.tool_registry.get_upstream_ids = MagicMock(
        return_value=list(extensions_by_upstream.keys()),
    )

    def get_self_description(upstream_id: str) -> UpstreamSelfDescription | None:
        ext = extensions_by_upstream.get(upstream_id)
        if ext is None:
            return None
        return UpstreamSelfDescription(
            name=upstream_id, version="1.0.0",
            capabilities_extensions=ext,
        )
    runtime.tool_registry.get_self_description = MagicMock(
        side_effect=get_self_description,
    )
    return runtime


def test_aggregated_extensions_single_upstream_passthrough() -> None:
    runtime = _make_runtime_with_extensions({
        "demo": {
            "io.modelcontextprotocol/ui": {
                "mimeTypes": ["text/html;profile=mcp-app"],
            },
        },
    })
    result = _aggregated_extensions([runtime])
    assert result == {
        "io.modelcontextprotocol/ui": {
            "mimeTypes": ["text/html;profile=mcp-app"],
        },
    }


def test_aggregated_extensions_unions_lists_across_upstreams() -> None:
    runtime = _make_runtime_with_extensions({
        "a": {
            "io.modelcontextprotocol/ui": {
                "mimeTypes": ["text/html;profile=mcp-app"],
            },
        },
        "b": {
            "io.modelcontextprotocol/ui": {
                "mimeTypes": ["application/json+widget"],
            },
        },
    })
    result = _aggregated_extensions([runtime])
    union = result["io.modelcontextprotocol/ui"]["mimeTypes"]
    assert sorted(union) == sorted(
        ["text/html;profile=mcp-app", "application/json+widget"],
    )


def test_aggregated_extensions_returns_empty_when_nothing_advertised() -> None:
    runtime = _make_runtime_with_extensions({"demo": {}})
    result = _aggregated_extensions([runtime])
    assert result == {}


# ─── Bare-name tool resolution (widget callbacks) ────────────────────


def _make_runtime_with_tools(
    tools_by_upstream: dict[str, list[str]],
    *,
    user_allowed_upstreams: list[str] | None = None,
) -> Any:
    """Build a fake runtime whose tool_registry returns the given
    discovered tools, and whose policy_engine allows the listed
    upstreams (or all when ``None``)."""
    runtime = MagicMock()
    runtime.tool_registry.get_upstream_ids = MagicMock(
        return_value=list(tools_by_upstream.keys()),
    )

    def get_tools_for_upstreams(ids: list[str]) -> list[DiscoveredTool]:
        out: list[DiscoveredTool] = []
        for uid in ids:
            for name in tools_by_upstream.get(uid, []):
                out.append(DiscoveredTool(
                    upstream_id=uid, original_name=name,
                    prefixed_name=f"{uid}__{name}",
                    description="d",
                    input_schema={"type": "object"},
                ))
        return out
    runtime.tool_registry.get_tools_for_upstreams = MagicMock(
        side_effect=get_tools_for_upstreams,
    )
    # get_allowed_upstreams always returns a set (no allow-all
    # sentinel): default to every upstream in the fixture.
    allowed = (
        set(tools_by_upstream.keys())
        if user_allowed_upstreams is None
        else set(user_allowed_upstreams)
    )
    runtime.policy_engine.get_allowed_upstreams = MagicMock(
        return_value=allowed,
    )
    return runtime


def test_resolve_bare_tool_name_returns_unique_match() -> None:
    """The widget→host bridge sends ``tools/call name="record_planet_click"``
    (no upstream prefix) because widgets have no idea they're being
    proxied. The gateway must find the unique upstream that owns the
    name and rewrite the call. Without this, MCP-Apps widgets that
    invoke their own tools (e.g. solar widget recording planet
    clicks) silently no-op against the gateway."""
    runtime = _make_runtime_with_tools({
        "demo": ["record_planet_click", "echo"],
        "other": ["echo"],
    })
    resolved = _resolve_bare_tool_name(
        runtime, user_id="alice", original_name="record_planet_click",
    )
    assert resolved == "demo__record_planet_click"


def test_resolve_bare_tool_name_errors_on_ambiguity() -> None:
    """Two upstreams expose ``echo`` — bare-name routing has no way to
    pick one. Surface an error rather than guessing wrong."""
    runtime = _make_runtime_with_tools({
        "demo": ["echo"],
        "other": ["echo"],
    })
    resolved = _resolve_bare_tool_name(
        runtime, user_id="alice", original_name="echo",
    )
    # CallToolResult error, not a string.
    assert not isinstance(resolved, str)
    text = resolved.content[0]
    assert getattr(text, "type", None) == "text"
    assert "Ambiguous" in getattr(text, "text", "")


def test_resolve_bare_tool_name_errors_when_unknown() -> None:
    runtime = _make_runtime_with_tools({"demo": ["echo"]})
    resolved = _resolve_bare_tool_name(
        runtime, user_id="alice", original_name="not_a_tool",
    )
    assert not isinstance(resolved, str)
    text = resolved.content[0]
    assert "Unknown tool" in getattr(text, "text", "")


# ─── Bare-URI resource resolution (widget readServerResource) ────────


def _make_runtime_with_resources(
    resources_by_upstream: dict[str, list[str]],
    *,
    user_allowed_upstreams: list[str] | None = None,
) -> Any:
    """Build a fake runtime whose tool_registry returns the given
    DiscoveredResource records, mirroring ``_make_runtime_with_tools``
    for the resources/read path."""
    runtime = MagicMock()
    runtime.tool_registry.get_upstream_ids = MagicMock(
        return_value=list(resources_by_upstream.keys()),
    )

    def get_resources_for_upstreams(ids: list[str]) -> list[DiscoveredResource]:
        out: list[DiscoveredResource] = []
        for uid in ids:
            for uri in resources_by_upstream.get(uid, []):
                out.append(DiscoveredResource(
                    upstream_id=uid, original_uri=uri, name=uri,
                ))
        return out
    runtime.tool_registry.get_resources_for_upstreams = MagicMock(
        side_effect=get_resources_for_upstreams,
    )
    # See _make_runtime_with_tools: no allow-all sentinel.
    allowed = (
        set(resources_by_upstream.keys())
        if user_allowed_upstreams is None
        else set(user_allowed_upstreams)
    )
    runtime.policy_engine.get_allowed_upstreams = MagicMock(
        return_value=allowed,
    )
    return runtime


def test_resolve_bare_resource_uri_returns_unique_match() -> None:
    """``app.readServerResource({uri: "ui://widget/data/foo"})`` from
    inside a widget passes the upstream-native URI to the gateway.
    Without bare-URI resolution that would 404 (the gateway expects
    wrapped URIs). The widget can't possibly know the wrapped form
    because it doesn't know it's being proxied."""
    runtime = _make_runtime_with_resources({
        "demo": ["ui://widget/data/foo", "test://hello"],
        "other": ["test://world"],
    })
    resolved = _resolve_bare_resource_uri(
        runtime, user_id="alice",
        original_uri="ui://widget/data/foo",
    )
    assert resolved == ("demo", "ui://widget/data/foo")


def test_resolve_bare_resource_uri_errors_on_ambiguity() -> None:
    runtime = _make_runtime_with_resources({
        "demo": ["ui://widget/data/foo"],
        "other": ["ui://widget/data/foo"],
    })
    resolved = _resolve_bare_resource_uri(
        runtime, user_id="alice",
        original_uri="ui://widget/data/foo",
    )
    assert isinstance(resolved, list)
    text = resolved[0].content
    assert isinstance(text, str)
    assert "Ambiguous" in text


def test_resolve_bare_resource_uri_errors_when_unknown() -> None:
    runtime = _make_runtime_with_resources({"demo": ["ui://widget/data/foo"]})
    resolved = _resolve_bare_resource_uri(
        runtime, user_id="alice",
        original_uri="ui://widget/data/missing",
    )
    assert isinstance(resolved, list)
    text = resolved[0].content
    assert isinstance(text, str)
    assert "Invalid resource URI" in text


def test_resolve_bare_resource_uri_respects_policy_engine() -> None:
    """Resources on a policy-disabled upstream are not reachable via
    the bare-URI fallback either — same access-control invariant as
    bare-name tool resolution."""
    runtime = _make_runtime_with_resources(
        {
            "allowed": ["ui://widget/data/foo"],
            "forbidden": ["ui://widget/data/foo"],
        },
        user_allowed_upstreams=["allowed"],
    )
    resolved = _resolve_bare_resource_uri(
        runtime, user_id="alice",
        original_uri="ui://widget/data/foo",
    )
    assert resolved == ("allowed", "ui://widget/data/foo")


def test_resolve_bare_tool_name_respects_policy_engine() -> None:
    """A tool exists on an upstream the user's role can't reach must
    not be reachable via the bare-name fallback either — otherwise
    the fallback is an access-control bypass."""
    runtime = _make_runtime_with_tools(
        {
            "allowed": ["record_planet_click"],
            "forbidden": ["record_planet_click"],
        },
        user_allowed_upstreams=["allowed"],
    )
    resolved = _resolve_bare_tool_name(
        runtime, user_id="alice", original_name="record_planet_click",
    )
    # ``forbidden`` is filtered out, so the unique remaining match is
    # ``allowed``. (Coincidentally also asserts that the fallback
    # honors policy filtering rather than scanning all upstreams.)
    assert resolved == "allowed__record_planet_click"


def test_aggregated_extensions_multi_org_unions_across_runtimes() -> None:
    """Multi-org: alice's runtime A advertises the ext, runtime B does
    not — gateway still returns the ext (so MCP-Apps clients in any
    membership see the marker)."""
    runtime_a = _make_runtime_with_extensions({
        "demo": {
            "io.modelcontextprotocol/ui": {
                "mimeTypes": ["text/html;profile=mcp-app"],
            },
        },
    })
    runtime_b = _make_runtime_with_extensions({"plain": {}})
    result = _aggregated_extensions([runtime_a, runtime_b])
    assert "io.modelcontextprotocol/ui" in result
