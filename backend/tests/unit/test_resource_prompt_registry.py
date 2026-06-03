"""Resource + prompt discovery in ``ToolRegistry``.

Mirrors ``test_tool_registry.py``: use ``UpstreamClientManager`` with
seeded mock sessions, drive the registry's ``_discover_resources`` /
``_discover_prompts`` directly, and pin the cache layout / pagination /
error-swallow semantics. The full ``refresh_all`` path is covered by
the integration test path (it's mostly orchestration); we focus here on
the per-upstream behavior that's load-bearing for downstream routing.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import mcp.types as mcp_types
import pytest
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.services.tool_registry import (
    ITEM_CAP_PER_UPSTREAM,
    ToolRegistry,
)
from tests.unit.factories import make_upstream_definition


def _make_resources_session(
    *,
    resources: list[mcp_types.Resource] | None = None,
    pages_resources: list[list[mcp_types.Resource]] | None = None,
    templates: list[mcp_types.ResourceTemplate] | None = None,
    pages_templates: list[list[mcp_types.ResourceTemplate]] | None = None,
    prompts: list[mcp_types.Prompt] | None = None,
    pages_prompts: list[list[mcp_types.Prompt]] | None = None,
    resources_error: Exception | None = None,
    templates_error: Exception | None = None,
    prompts_error: Exception | None = None,
) -> Any:
    """Build a stub ``ClientSession`` that emits the requested pages.

    For each surface (resources / templates / prompts), pass either a
    single list (one page) or ``pages_*`` (a sequence of pages joined by
    a synthetic cursor). ``*_error`` overrides the surface to raise the
    given exception on every call — used for "swallow" tests.
    """
    session = MagicMock()

    def _paginated(items_or_pages: list[Any] | list[list[Any]] | None) -> Any:
        if items_or_pages is None:
            return None
        # Detect single-page call: a flat list, not list-of-lists.
        if items_or_pages and not isinstance(items_or_pages[0], list):
            pages = [items_or_pages]
        else:
            pages = items_or_pages  # type: ignore[assignment]
        return pages

    res_pages = _paginated(pages_resources or resources)
    tpl_pages = _paginated(pages_templates or templates)
    prompt_pages = _paginated(pages_prompts or prompts)

    async def list_resources(
        *, params: mcp_types.PaginatedRequestParams | None = None,
    ) -> mcp_types.ListResourcesResult:
        if resources_error is not None:
            raise resources_error
        if not res_pages:
            return mcp_types.ListResourcesResult(resources=[])
        cursor = params.cursor if params is not None else None
        idx = 0 if cursor is None else int(cursor)
        page = res_pages[idx] if idx < len(res_pages) else []
        next_cursor = str(idx + 1) if idx + 1 < len(res_pages) else None
        return mcp_types.ListResourcesResult(
            resources=page, nextCursor=next_cursor,
        )

    async def list_resource_templates(
        *, params: mcp_types.PaginatedRequestParams | None = None,
    ) -> mcp_types.ListResourceTemplatesResult:
        if templates_error is not None:
            raise templates_error
        if not tpl_pages:
            return mcp_types.ListResourceTemplatesResult(resourceTemplates=[])
        cursor = params.cursor if params is not None else None
        idx = 0 if cursor is None else int(cursor)
        page = tpl_pages[idx] if idx < len(tpl_pages) else []
        next_cursor = str(idx + 1) if idx + 1 < len(tpl_pages) else None
        return mcp_types.ListResourceTemplatesResult(
            resourceTemplates=page, nextCursor=next_cursor,
        )

    async def list_prompts(
        *, params: mcp_types.PaginatedRequestParams | None = None,
    ) -> mcp_types.ListPromptsResult:
        if prompts_error is not None:
            raise prompts_error
        if not prompt_pages:
            return mcp_types.ListPromptsResult(prompts=[])
        cursor = params.cursor if params is not None else None
        idx = 0 if cursor is None else int(cursor)
        page = prompt_pages[idx] if idx < len(prompt_pages) else []
        next_cursor = str(idx + 1) if idx + 1 < len(prompt_pages) else None
        return mcp_types.ListPromptsResult(
            prompts=page, nextCursor=next_cursor,
        )

    session.list_resources = AsyncMock(side_effect=list_resources)
    session.list_resource_templates = AsyncMock(
        side_effect=list_resource_templates
    )
    session.list_prompts = AsyncMock(side_effect=list_prompts)
    return session


def _make_resource(uri: str, name: str = "page") -> mcp_types.Resource:
    return mcp_types.Resource(
        uri=AnyUrl(uri), name=name, description="A page",
        mimeType="text/plain",
    )


def _make_template(uri_template: str, name: str = "tmpl") -> mcp_types.ResourceTemplate:
    return mcp_types.ResourceTemplate(
        uriTemplate=uri_template, name=name, description="A template",
    )


def _make_prompt(name: str) -> mcp_types.Prompt:
    return mcp_types.Prompt(
        name=name,
        description=f"prompt {name}",
        arguments=[
            mcp_types.PromptArgument(name="who", required=True),
        ],
    )


def make_registry_with_session(
    upstream_id: str = "notion",
    *,
    session: Any | None = None,
) -> tuple[ToolRegistry, UpstreamClientManager]:
    upstream = make_upstream_definition(id=upstream_id)
    client_manager = UpstreamClientManager([upstream])
    if session is not None:
        from tests.unit._state_seed import seed_shared_session
        seed_shared_session(client_manager, upstream_id, session=session)
    registry = ToolRegistry([upstream], client_manager)
    return registry, client_manager


@pytest.mark.asyncio
async def test_discover_resources_collects_resources_and_templates() -> None:
    session = _make_resources_session(
        resources=[
            _make_resource("test://hello", name="hello"),
            _make_resource("test://world", name="world"),
        ],
        templates=[_make_template("test://things/{id}", name="thing")],
    )
    registry, _ = make_registry_with_session(session=session)
    resources, templates = await registry._discover_resources("notion")  # pyright: ignore[reportPrivateUsage]

    assert [r.original_uri for r in resources] == [
        "test://hello", "test://world",
    ]
    assert [r.upstream_id for r in resources] == ["notion", "notion"]
    assert resources[0].name == "hello"
    assert resources[0].mime_type == "text/plain"

    assert [t.original_uri_template for t in templates] == [
        "test://things/{id}",
    ]
    assert templates[0].name == "thing"


@pytest.mark.asyncio
async def test_discover_prompts_collects_arguments() -> None:
    session = _make_resources_session(
        prompts=[_make_prompt("greet")],
    )
    registry, _ = make_registry_with_session(session=session)
    prompts = await registry._discover_prompts("notion")  # pyright: ignore[reportPrivateUsage]

    assert [p.original_name for p in prompts] == ["greet"]
    assert prompts[0].prefixed_name == "notion__greet"
    assert [a.name for a in prompts[0].arguments] == ["who"]
    assert prompts[0].arguments[0].required is True


@pytest.mark.asyncio
async def test_discover_resources_swallows_errors() -> None:
    """Upstreams that don't declare a resources capability often respond
    to ``resources/list`` with a protocol error. The registry must log
    and swallow so a missing surface on one upstream doesn't blow up
    the whole discovery pass for everyone else."""
    session = _make_resources_session(
        resources_error=RuntimeError("MethodNotFound"),
        templates_error=RuntimeError("MethodNotFound"),
    )
    registry, _ = make_registry_with_session(session=session)
    resources, templates = await registry._discover_resources("notion")  # pyright: ignore[reportPrivateUsage]
    assert resources == []
    assert templates == []


@pytest.mark.asyncio
async def test_discover_prompts_swallows_errors() -> None:
    session = _make_resources_session(
        prompts_error=RuntimeError("MethodNotFound"),
    )
    registry, _ = make_registry_with_session(session=session)
    prompts = await registry._discover_prompts("notion")  # pyright: ignore[reportPrivateUsage]
    assert prompts == []


def _method_not_found() -> McpError:
    return McpError(
        mcp_types.ErrorData(
            code=mcp_types.METHOD_NOT_FOUND, message="not supported",
        ),
    )


def _transport_stalls() -> list[Exception]:
    """Errors that mean the session's transport is unusable — each must
    PROPAGATE out of discovery so the caller reconnects on a fresh
    transport (the E2B post-reattach stall surfaces as these)."""
    return [
        asyncio.TimeoutError(),
        anyio.BrokenResourceError(),
        anyio.ClosedResourceError(),
        McpError(
            mcp_types.ErrorData(
                code=mcp_types.CONNECTION_CLOSED, message="connection closed",
            ),
        ),
    ]


@pytest.mark.parametrize("stall", _transport_stalls())
@pytest.mark.asyncio
async def test_discover_resources_propagates_transport_stall(
    stall: Exception,
) -> None:
    session = _make_resources_session(resources_error=stall)
    registry, _ = make_registry_with_session(session=session)
    with pytest.raises(type(stall)):
        await registry._discover_resources("notion")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("stall", _transport_stalls())
@pytest.mark.asyncio
async def test_discover_prompts_propagates_transport_stall(
    stall: Exception,
) -> None:
    session = _make_resources_session(prompts_error=stall)
    registry, _ = make_registry_with_session(session=session)
    with pytest.raises(type(stall)):
        await registry._discover_prompts("notion")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_discover_resources_swallows_method_not_found() -> None:
    """A genuine ``MethodNotFound`` (server answered: it doesn't support
    the surface) is NOT a transport stall — swallow it as empty, don't
    propagate (that would trigger a pointless fresh reconnect)."""
    session = _make_resources_session(
        resources_error=_method_not_found(),
        templates_error=_method_not_found(),
    )
    registry, _ = make_registry_with_session(session=session)
    resources, templates = await registry._discover_resources("notion")  # pyright: ignore[reportPrivateUsage]
    assert resources == []
    assert templates == []


@pytest.mark.asyncio
async def test_discover_resources_exhausts_pagination() -> None:
    page_a = [_make_resource(f"test://page/{i}", name=f"p{i}") for i in range(5)]
    page_b = [_make_resource(f"test://page/{i}", name=f"p{i}") for i in range(5, 10)]
    session = _make_resources_session(
        pages_resources=[page_a, page_b],
    )
    registry, _ = make_registry_with_session(session=session)
    resources, _ = await registry._discover_resources("notion")  # pyright: ignore[reportPrivateUsage]
    assert len(resources) == 10
    assert resources[0].original_uri == "test://page/0"
    assert resources[-1].original_uri == "test://page/9"


@pytest.mark.asyncio
async def test_discover_resources_caps_at_item_limit() -> None:
    """A misbehaving upstream that advertises more pages than the cap
    must be truncated — and a WARNING surfaces in logs so the
    truncation is visible to operators."""
    over_cap = [
        _make_resource(f"test://r/{i}", name=f"r{i}")
        for i in range(ITEM_CAP_PER_UPSTREAM + 5)
    ]
    session = _make_resources_session(resources=over_cap)
    registry, _ = make_registry_with_session(session=session)
    resources, _ = await registry._discover_resources("notion")  # pyright: ignore[reportPrivateUsage]
    assert len(resources) == ITEM_CAP_PER_UPSTREAM


@pytest.mark.asyncio
async def test_refresh_all_aggregates_across_surfaces() -> None:
    from tests.unit._state_seed import seed_shared_session
    upstream_a = make_upstream_definition(id="a")
    upstream_b = make_upstream_definition(id="b")
    cm = UpstreamClientManager([upstream_a, upstream_b])

    sess_a = _make_resources_session(
        resources=[_make_resource("test://from-a")],
        prompts=[_make_prompt("p_a")],
    )
    sess_a.list_tools = AsyncMock(return_value=mcp_types.ListToolsResult(tools=[]))
    seed_shared_session(cm, "a", session=sess_a)
    sess_b = _make_resources_session(
        resources=[_make_resource("test://from-b")],
        prompts=[_make_prompt("p_b")],
    )
    sess_b.list_tools = AsyncMock(return_value=mcp_types.ListToolsResult(tools=[]))
    seed_shared_session(cm, "b", session=sess_b)

    registry = ToolRegistry([upstream_a, upstream_b], cm)
    await registry.refresh_all()

    resources = registry.get_resources_for_upstreams(["a", "b"])
    prompts = registry.get_prompts_for_upstreams(["a", "b"])
    assert sorted(r.original_uri for r in resources) == [
        "test://from-a", "test://from-b",
    ]
    assert sorted(p.prefixed_name for p in prompts) == [
        "a__p_a", "b__p_b",
    ]


@pytest.mark.asyncio
async def test_refresh_upstream_replaces_only_one_upstreams_entries() -> None:
    from tests.unit._state_seed import seed_shared_session
    upstream_a = make_upstream_definition(id="a")
    upstream_b = make_upstream_definition(id="b")
    cm = UpstreamClientManager([upstream_a, upstream_b])

    sess_a_v1 = _make_resources_session(
        resources=[_make_resource("test://from-a/v1")],
        prompts=[_make_prompt("p_a_v1")],
    )
    sess_a_v1.list_tools = AsyncMock(return_value=mcp_types.ListToolsResult(tools=[]))
    seed_shared_session(cm, "a", session=sess_a_v1)
    sess_b = _make_resources_session(
        resources=[_make_resource("test://from-b")],
        prompts=[_make_prompt("p_b")],
    )
    sess_b.list_tools = AsyncMock(return_value=mcp_types.ListToolsResult(tools=[]))
    seed_shared_session(cm, "b", session=sess_b)

    registry = ToolRegistry([upstream_a, upstream_b], cm)
    await registry.refresh_all()

    # Now have ``a`` advertise a different set, leave ``b`` alone.
    sess_a_v2 = _make_resources_session(
        resources=[_make_resource("test://from-a/v2")],
        prompts=[_make_prompt("p_a_v2")],
    )
    sess_a_v2.list_tools = AsyncMock(return_value=mcp_types.ListToolsResult(tools=[]))
    seed_shared_session(cm, "a", session=sess_a_v2)

    await registry.refresh_upstream("a")

    a_resources = registry.get_resources_for_upstreams(["a"])
    b_resources = registry.get_resources_for_upstreams(["b"])
    a_prompts = registry.get_prompts_for_upstreams(["a"])
    b_prompts = registry.get_prompts_for_upstreams(["b"])

    assert [r.original_uri for r in a_resources] == ["test://from-a/v2"]
    assert [r.original_uri for r in b_resources] == ["test://from-b"]
    assert [p.original_name for p in a_prompts] == ["p_a_v2"]
    assert [p.original_name for p in b_prompts] == ["p_b"]


def test_resolve_prompt_returns_upstream_and_name() -> None:
    upstream = make_upstream_definition(id="notion")
    registry = ToolRegistry([upstream], MagicMock())
    assert registry.resolve_prompt("notion__greet") == ("notion", "greet")


def test_resolve_prompt_returns_none_for_unknown() -> None:
    upstream = make_upstream_definition(id="notion")
    registry = ToolRegistry([upstream], MagicMock())
    assert registry.resolve_prompt("unknown__greet") is None
    assert registry.resolve_prompt("no-separator") is None
