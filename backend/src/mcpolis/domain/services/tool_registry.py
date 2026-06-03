from __future__ import annotations

import asyncio
import time
from typing import Any

import anyio
import mcp.types as mcp_types
import structlog
from mcp.shared.exceptions import McpError

from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.upstream import (
    DiscoveredPrompt,
    DiscoveredResource,
    DiscoveredResourceTemplate,
    DiscoveredTool,
    PromptArgument,
    ToolAnnotations,
    UpstreamDefinition,
    UpstreamSelfDescription,
)
from mcpolis.domain.ports.tool_catalog_repository import (
    ToolCatalogRepository,
    ToolCatalogSnapshot,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

SEPARATOR = "__"
# Joins an upstream's display name onto a tool/resource/prompt title so
# clients can tell same-named items from different upstreams apart
# (e.g. "Mixpanel: Get Projects" vs "Mixpanel2: Get Projects"). Distinct
# from ``SEPARATOR``: a title is display-only and never parsed back, so
# this can be human-friendly without breaking routing.
DISPLAY_TITLE_SEPARATOR = ": "


def prefix_display_title(display_name: str, title: str | None) -> str | None:
    """Prefix a human-facing *title* with the upstream's display name.

    Returns *title* unchanged when it's falsy: an item with no title
    falls back to its (already upstream-prefixed) ``name`` in clients, so
    the upstream stays visible without us synthesizing a title — which
    would otherwise hide the real callable name. Applied only at
    wire-build time; the stored ``Discovered*`` record keeps the
    upstream's original title, so rebuilding on each list call never
    double-prefixes.
    """
    if not title:
        return title
    return f"{display_name}{DISPLAY_TITLE_SEPARATOR}{title}"
# Per-call caps on a single list round-trip. Discovery answers in well
# under a second even over a sandbox hop, so a multi-second silence means
# the transport has stalled (see ``is_transport_stall`` / the E2B
# reattach stall) — short enough to detect+recover fast, generous enough
# not to false-positive on a momentarily busy server.
LIST_TOOLS_TIMEOUT = 15.0
LIST_RESOURCES_TIMEOUT = 15.0
LIST_PROMPTS_TIMEOUT = 15.0


def is_transport_stall(exc: BaseException) -> bool:
    """True when *exc* means "the session's transport is unusable", as
    opposed to a normal server-side error response.

    Recoverable by reconnecting on a fresh transport: a per-call timeout
    (no response at all), a closed/broken in-memory stream, or the MCP
    SDK's ``CONNECTION_CLOSED`` (read loop ended). A plain ``McpError``
    with any other code means the server *answered* with an error — the
    transport is fine, so we don't reconnect (and ``MethodNotFound`` for
    an unsupported list method is swallowed as "empty" by the callers).
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, (anyio.BrokenResourceError, anyio.ClosedResourceError)):
        return True
    if isinstance(exc, McpError):
        return exc.error.code == mcp_types.CONNECTION_CLOSED
    return False


# Per-upstream cap on items returned from a paginated list call. Match
# the existing tool-discovery model: exhaust the cursor chain in
# ``refresh_all`` rather than per-request, but stop early at this cap so
# a misbehaving upstream cannot exhaust memory.
ITEM_CAP_PER_UPSTREAM = 1000


class ToolRegistry:
    def __init__(
        self,
        upstreams: list[UpstreamDefinition],
        client_manager: UpstreamClientManager,
        catalog_repo: ToolCatalogRepository | None = None,
        org_id: str | None = None,
    ) -> None:
        self._upstreams = {u.id: u for u in upstreams}
        self._client_manager = client_manager
        self._tools: list[DiscoveredTool] = []
        self._resources: list[DiscoveredResource] = []
        self._resource_templates: list[DiscoveredResourceTemplate] = []
        self._prompts: list[DiscoveredPrompt] = []
        # Persistent catalog so the admin permissions UI works across
        # restarts without an OAuth reconnect. ``None`` in unit tests
        # that don't care about persistence; production paths always
        # wire one in via ``OrgRuntimeManager``.
        self._catalog_repo = catalog_repo
        self._org_id = org_id
        self._hydrated = False
        # Upstream IDs whose post-connect catalog refresh is in flight.
        # Marked by ``connect_and_refresh_tools`` only — startup
        # reconnect, periodic refresh, and SDK-pushed list_changed
        # paths deliberately don't flip this so the dashboard "Fetching
        # info" pill stays a signal of user-driven connect/reconnect
        # work, not background catalog upkeep. The companion timestamp
        # map lets the background helper enforce a minimum display
        # window so a fast refresh doesn't flash the pill and look like
        # a glitch.
        self._refreshing: set[str] = set()
        self._refreshing_started_at: dict[str, float] = {}

    def is_refreshing(self, upstream_id: str) -> bool:
        return upstream_id in self._refreshing

    def refreshing_started_at(self, upstream_id: str) -> float | None:
        return self._refreshing_started_at.get(upstream_id)

    def mark_refreshing(self, upstream_id: str) -> None:
        self._refreshing.add(upstream_id)
        self._refreshing_started_at[upstream_id] = time.monotonic()

    def unmark_refreshing(self, upstream_id: str) -> None:
        self._refreshing.discard(upstream_id)
        self._refreshing_started_at.pop(upstream_id, None)

    async def hydrate(self) -> None:
        """Load any persisted catalog from the repository.

        Idempotent. Called by the runtime manager before the first
        ``refresh_all`` so cold-start admin views see the cached tool
        list even when no upstream has reconnected yet. The hydrated
        per-upstream slices remain in place for upstreams that
        ``refresh_all`` does not touch (i.e. those not currently
        connected).
        """
        if self._hydrated:
            return
        self._hydrated = True
        if self._catalog_repo is None or self._org_id is None:
            return
        snapshots = await self._catalog_repo.load_all(self._org_id)
        for upstream_id, snap in snapshots.items():
            if upstream_id not in self._upstreams:
                continue
            self._tools.extend(snap.tools)
            self._resources.extend(snap.resources)
            self._resource_templates.extend(snap.resource_templates)
            self._prompts.extend(snap.prompts)
        logger.info(
            "tool.registry.hydrated",
            org_id=self._org_id,
            upstream_count=len(snapshots),
            tool_count=len(self._tools),
        )

    def _snapshot_for(self, upstream_id: str) -> ToolCatalogSnapshot:
        return ToolCatalogSnapshot(
            tools=[t for t in self._tools if t.upstream_id == upstream_id],
            resources=[
                r for r in self._resources if r.upstream_id == upstream_id
            ],
            resource_templates=[
                t for t in self._resource_templates
                if t.upstream_id == upstream_id
            ],
            prompts=[p for p in self._prompts if p.upstream_id == upstream_id],
        )

    async def _persist_upstream(self, upstream_id: str) -> None:
        if self._catalog_repo is None or self._org_id is None:
            return
        try:
            await self._catalog_repo.upsert_upstream(
                self._org_id, upstream_id, self._snapshot_for(upstream_id),
            )
        except Exception:
            logger.exception(
                "tool.registry.persist.failed",
                org_id=self._org_id,
                upstream_id=upstream_id,
            )

    async def _delete_persisted(self, upstream_id: str) -> None:
        if self._catalog_repo is None or self._org_id is None:
            return
        try:
            await self._catalog_repo.delete_upstream(self._org_id, upstream_id)
        except Exception:
            logger.exception(
                "tool.registry.delete.failed",
                org_id=self._org_id,
                upstream_id=upstream_id,
            )

    async def refresh_all(self) -> None:
        """Re-discover catalogue for every currently connected upstream.

        Per-upstream rather than wholesale: hydrated state for
        non-connected upstreams (e.g. OAuth ones whose tokens have not
        been reconnected this restart) stays untouched, so the admin
        permissions UI keeps showing the cached tool list. Each
        successful per-upstream refresh writes through to the catalog
        repository.
        """
        connected = list(self._client_manager.connected_upstream_ids)
        for upstream_id in connected:
            try:
                await self.refresh_upstream(upstream_id)
            except Exception:
                logger.exception(
                    "tool.registry.refresh.failed",
                    upstream_id=upstream_id,
                )
        logger.info(
            "tool.registry.refresh.completed",
            tool_count=len(self._tools),
            resource_count=len(self._resources),
            template_count=len(self._resource_templates),
            prompt_count=len(self._prompts),
            upstream_count=len(self._upstreams),
            refreshed=len(connected),
        )

    def _resolve_discovery_session(self, upstream_id: str):  # type: ignore[no-untyped-def]
        """Pick the session used for upstream discovery calls.

        Order:
        1. Any per-user session for the upstream (admin_oauth lives
           here under the slot owner's real email; per_user_oauth
           sessions belong to individual users).
        2. The shared service-account session.

        Pulled out so every discovery surface (tools, resources,
        prompts) sees the same toolset / catalogue and does not drift.
        """
        any_user = self._client_manager.any_user_session_for_upstream(
            upstream_id,
        )
        if any_user is not None:
            return any_user
        return self._client_manager.get_session(upstream_id)

    async def _discover_upstream(self, upstream_id: str) -> list[DiscoveredTool]:
        # For OAuth upstreams, try the admin's per-user session first
        session = self._resolve_discovery_session(upstream_id)
        result = await asyncio.wait_for(
            session.list_tools(), timeout=LIST_TOOLS_TIMEOUT
        )
        tools: list[DiscoveredTool] = []
        for tool in result.tools:
            description = tool.description
            annotations: ToolAnnotations | None = None
            if tool.annotations:
                annotations = ToolAnnotations(
                    title=tool.annotations.title,
                    readOnlyHint=tool.annotations.readOnlyHint,
                    destructiveHint=tool.annotations.destructiveHint,
                    idempotentHint=tool.annotations.idempotentHint,
                    openWorldHint=tool.annotations.openWorldHint,
                )
            tools.append(
                DiscoveredTool(
                    upstream_id=upstream_id,
                    original_name=tool.name,
                    prefixed_name=f"{upstream_id}{SEPARATOR}{tool.name}",
                    description=description,
                    input_schema=tool.inputSchema,
                    title=tool.title,
                    output_schema=tool.outputSchema,
                    annotations=annotations,
                    meta=tool.meta,
                )
            )
        return tools

    async def _discover_resources(
        self, upstream_id: str,
    ) -> tuple[list[DiscoveredResource], list[DiscoveredResourceTemplate]]:
        """List resources + resource templates for one upstream.

        Pagination is exhausted in-place up to ``ITEM_CAP_PER_UPSTREAM``;
        on a misbehaving upstream that advertises an infinite cursor, the
        cap kicks in with a WARNING log so the truncation is visible.
        ``MethodNotFound`` (upstream didn't declare the capability) is
        logged at INFO and swallowed — same shape as the tool path
        logs ``tool.registry.refresh.failed`` for any other error.
        """
        session = self._resolve_discovery_session(upstream_id)

        resources: list[DiscoveredResource] = []
        try:
            cursor: str | None = None
            while True:
                params = (
                    mcp_types.PaginatedRequestParams(cursor=cursor)
                    if cursor is not None
                    else None
                )
                result = await asyncio.wait_for(
                    session.list_resources(params=params),
                    timeout=LIST_RESOURCES_TIMEOUT,
                )
                for r in result.resources:
                    resources.append(
                        DiscoveredResource(
                            upstream_id=upstream_id,
                            original_uri=str(r.uri),
                            name=r.name,
                            title=r.title,
                            description=r.description,
                            mime_type=r.mimeType,
                            meta=r.meta,
                        )
                    )
                    if len(resources) >= ITEM_CAP_PER_UPSTREAM:
                        logger.warning(
                            "resource.registry.cap_reached",
                            upstream_id=upstream_id,
                            cap=ITEM_CAP_PER_UPSTREAM,
                        )
                        break
                if (
                    len(resources) >= ITEM_CAP_PER_UPSTREAM
                    or result.nextCursor is None
                ):
                    break
                cursor = result.nextCursor
        except Exception as exc:
            if is_transport_stall(exc):
                raise
            logger.info(
                "resource.registry.list_failed",
                upstream_id=upstream_id,
            )

        templates: list[DiscoveredResourceTemplate] = []
        try:
            cursor = None
            while True:
                t_params = (
                    mcp_types.PaginatedRequestParams(cursor=cursor)
                    if cursor is not None
                    else None
                )
                t_result = await asyncio.wait_for(
                    session.list_resource_templates(params=t_params),
                    timeout=LIST_RESOURCES_TIMEOUT,
                )
                for t in t_result.resourceTemplates:
                    templates.append(
                        DiscoveredResourceTemplate(
                            upstream_id=upstream_id,
                            original_uri_template=t.uriTemplate,
                            name=t.name,
                            title=t.title,
                            description=t.description,
                            mime_type=t.mimeType,
                            meta=t.meta,
                        )
                    )
                    if len(templates) >= ITEM_CAP_PER_UPSTREAM:
                        logger.warning(
                            "resource_template.registry.cap_reached",
                            upstream_id=upstream_id,
                            cap=ITEM_CAP_PER_UPSTREAM,
                        )
                        break
                if (
                    len(templates) >= ITEM_CAP_PER_UPSTREAM
                    or t_result.nextCursor is None
                ):
                    break
                cursor = t_result.nextCursor
        except Exception as exc:
            if is_transport_stall(exc):
                raise
            logger.info(
                "resource_template.registry.list_failed",
                upstream_id=upstream_id,
            )

        return resources, templates

    async def _discover_prompts(
        self, upstream_id: str,
    ) -> list[DiscoveredPrompt]:
        """List prompts for one upstream. Pagination + ``MethodNotFound``
        handling mirrors ``_discover_resources``."""
        session = self._resolve_discovery_session(upstream_id)
        prompts: list[DiscoveredPrompt] = []
        try:
            cursor: str | None = None
            while True:
                params = (
                    mcp_types.PaginatedRequestParams(cursor=cursor)
                    if cursor is not None
                    else None
                )
                result = await asyncio.wait_for(
                    session.list_prompts(params=params),
                    timeout=LIST_PROMPTS_TIMEOUT,
                )
                for p in result.prompts:
                    arguments: list[PromptArgument] = []
                    if p.arguments:
                        for arg in p.arguments:
                            arguments.append(
                                PromptArgument(
                                    name=arg.name,
                                    description=arg.description,
                                    required=arg.required,
                                )
                            )
                    prompts.append(
                        DiscoveredPrompt(
                            upstream_id=upstream_id,
                            original_name=p.name,
                            prefixed_name=f"{upstream_id}{SEPARATOR}{p.name}",
                            title=p.title,
                            description=p.description,
                            arguments=arguments,
                            meta=p.meta,
                        )
                    )
                    if len(prompts) >= ITEM_CAP_PER_UPSTREAM:
                        logger.warning(
                            "prompt.registry.cap_reached",
                            upstream_id=upstream_id,
                            cap=ITEM_CAP_PER_UPSTREAM,
                        )
                        break
                if (
                    len(prompts) >= ITEM_CAP_PER_UPSTREAM
                    or result.nextCursor is None
                ):
                    break
                cursor = result.nextCursor
        except Exception as exc:
            if is_transport_stall(exc):
                raise
            logger.info(
                "prompt.registry.list_failed",
                upstream_id=upstream_id,
            )
        return prompts

    def get_upstream_ids(self) -> list[str]:
        """Return all configured upstream IDs."""
        return list(self._upstreams.keys())

    def get_upstream_definition(
        self, upstream_id: str,
    ) -> UpstreamDefinition | None:
        """Return the registered ``UpstreamDefinition`` for *upstream_id*.

        Tracks the live registration set kept in sync with
        ``register_upstream`` / ``unregister_upstream``, so consumers
        see upstreams added after runtime construction (the
        ``runtime.upstreams`` list is frozen at construction time).
        """
        return self._upstreams.get(upstream_id)

    def display_name_for(self, upstream_id: str) -> str:
        """Human display name for *upstream_id*, falling back to the id.

        Single source of truth for how an upstream is labelled when its
        name is folded into a tool / resource / prompt title, so every
        aggregated surface disambiguates the same way.
        """
        defn = self._upstreams.get(upstream_id)
        if defn is not None and defn.display_name:
            return defn.display_name
        return upstream_id

    def get_all_tools(self) -> list[DiscoveredTool]:
        return list(self._tools)

    def get_tools_for_upstreams(self, upstream_ids: list[str]) -> list[DiscoveredTool]:
        allowed = set(upstream_ids)
        return [t for t in self._tools if t.upstream_id in allowed]

    def get_resources_for_upstreams(
        self, upstream_ids: list[str],
    ) -> list[DiscoveredResource]:
        allowed = set(upstream_ids)
        return [r for r in self._resources if r.upstream_id in allowed]

    def get_resource_templates_for_upstreams(
        self, upstream_ids: list[str],
    ) -> list[DiscoveredResourceTemplate]:
        allowed = set(upstream_ids)
        return [
            t for t in self._resource_templates if t.upstream_id in allowed
        ]

    def get_prompts_for_upstreams(
        self, upstream_ids: list[str],
    ) -> list[DiscoveredPrompt]:
        allowed = set(upstream_ids)
        return [p for p in self._prompts if p.upstream_id in allowed]

    def get_self_description(
        self, upstream_id: str,
    ) -> UpstreamSelfDescription | None:
        """Return the upstream's captured ``initialize`` self-description.

        Thin pass-through to ``UpstreamClientManager.get_self_description``
        so the gateway controller can fold descriptions into the gateway's
        own ``instructions`` text without reaching past the registry.
        """
        return self._client_manager.get_self_description(upstream_id)

    def get_tool_annotations(self, upstream_id: str, tool_name: str) -> dict[str, bool]:
        """Return annotation flags for a tool, or empty dict if not found."""
        for t in self._tools:
            if t.upstream_id == upstream_id and t.original_name == tool_name:
                return t.annotations.to_flags() if t.annotations else {}
        return {}

    def resolve_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        """Return (upstream_id, original_tool_name) or None if not found."""
        parts = prefixed_name.split(SEPARATOR, 1)
        if len(parts) != 2:
            return None
        upstream_id, original_name = parts
        if upstream_id not in self._upstreams:
            return None
        return upstream_id, original_name

    def resolve_prompt(self, prefixed_name: str) -> tuple[str, str] | None:
        """Return (upstream_id, original_prompt_name) or None if not found."""
        parts = prefixed_name.split(SEPARATOR, 1)
        if len(parts) != 2:
            return None
        upstream_id, original_name = parts
        if upstream_id not in self._upstreams:
            return None
        return upstream_id, original_name

    def register_upstream(self, upstream: UpstreamDefinition) -> None:
        self._upstreams[upstream.id] = upstream

    async def unregister_upstream(self, upstream_id: str) -> None:
        self._upstreams.pop(upstream_id, None)
        self._tools = [t for t in self._tools if t.upstream_id != upstream_id]
        self._resources = [
            r for r in self._resources if r.upstream_id != upstream_id
        ]
        self._resource_templates = [
            t for t in self._resource_templates if t.upstream_id != upstream_id
        ]
        self._prompts = [
            p for p in self._prompts if p.upstream_id != upstream_id
        ]
        await self._delete_persisted(upstream_id)

    async def refresh_upstream(self, upstream_id: str) -> list[DiscoveredTool]:
        """Re-discover tools / resources / prompts for one upstream.

        Tools are returned for parity with the previous signature (the
        notifier path uses the count). Resources, templates, and prompts
        are also refreshed so a forwarded ``resources/list_changed`` /
        ``prompts/list_changed`` from upstream stays in sync. The
        resulting per-upstream slice is written through to the catalog
        repository so it survives a restart.

        Each discovery phase is timed and emits a
        ``tool.registry.refresh_upstream.phase`` log line. The three
        phases (tools / resources+templates / prompts) run concurrently
        via ``asyncio.gather`` so a single hung surface caps the refresh
        at one per-call timeout instead of the sum — before this, a
        server that stalled on resources AND prompts cost ~90s (3×30s)
        for what is really one failure. Tool discovery failing still
        fails the whole refresh (the caller surfaces it); resources and
        prompts are best-effort and only logged on error.
        """
        overall_start = time.monotonic()

        async def _timed_tools() -> list[DiscoveredTool]:
            phase_start = time.monotonic()
            tools = await self._discover_upstream(upstream_id)
            logger.info(
                "tool.registry.refresh_upstream.phase",
                upstream_id=upstream_id,
                phase="list_tools",
                duration_ms=int((time.monotonic() - phase_start) * 1000),
                count=len(tools),
            )
            return tools

        async def _timed_resources() -> tuple[
            list[DiscoveredResource], list[DiscoveredResourceTemplate],
        ]:
            phase_start = time.monotonic()
            res, templates = await self._discover_resources(upstream_id)
            logger.info(
                "tool.registry.refresh_upstream.phase",
                upstream_id=upstream_id,
                phase="list_resources",
                duration_ms=int((time.monotonic() - phase_start) * 1000),
                resource_count=len(res),
                template_count=len(templates),
            )
            return res, templates

        async def _timed_prompts() -> list[DiscoveredPrompt]:
            phase_start = time.monotonic()
            prompts = await self._discover_prompts(upstream_id)
            logger.info(
                "tool.registry.refresh_upstream.phase",
                upstream_id=upstream_id,
                phase="list_prompts",
                duration_ms=int((time.monotonic() - phase_start) * 1000),
                count=len(prompts),
            )
            return prompts

        tools_res, resources_res, prompts_res = await asyncio.gather(
            _timed_tools(), _timed_resources(), _timed_prompts(),
            return_exceptions=True,
        )

        # Any exception escaping a phase is raised (CancelledError too —
        # it's a BaseException). The discovery helpers already swallow
        # normal server-side outcomes (MethodNotFound → empty), so what's
        # left is a transport stall: raise it rather than persist a
        # half-empty catalogue off a stalled session. The caller
        # (``acquire_and_refresh_with_recovery``) reconnects on a fresh
        # transport and retries. Tools is checked first to preserve the
        # "tools failure fails the refresh" contract.
        if isinstance(tools_res, BaseException):
            raise tools_res
        if isinstance(resources_res, BaseException):
            raise resources_res
        if isinstance(prompts_res, BaseException):
            raise prompts_res

        new_tools = tools_res
        new_resources, new_templates = resources_res
        new_prompts = prompts_res
        self._tools = [
            t for t in self._tools if t.upstream_id != upstream_id
        ] + new_tools
        self._resources = [
            r for r in self._resources if r.upstream_id != upstream_id
        ] + new_resources
        self._resource_templates = [
            t for t in self._resource_templates
            if t.upstream_id != upstream_id
        ] + new_templates
        self._prompts = [
            p for p in self._prompts if p.upstream_id != upstream_id
        ] + new_prompts

        await self._persist_upstream(upstream_id)
        logger.info(
            "tool.registry.refresh_upstream.completed",
            upstream_id=upstream_id,
            total_duration_ms=int((time.monotonic() - overall_start) * 1000),
        )
        return new_tools

    async def refresh_resources_for_upstream(
        self, upstream_id: str,
    ) -> list[DiscoveredResource]:
        """Re-discover only resources + templates for one upstream.

        The PolicyNotifier resources-changed path uses this so an upstream
        push that only mentions resources doesn't pay for a full
        tool/prompt re-list.
        """
        new_resources, new_templates = await self._discover_resources(
            upstream_id,
        )
        self._resources = [
            r for r in self._resources if r.upstream_id != upstream_id
        ] + new_resources
        self._resource_templates = [
            t for t in self._resource_templates
            if t.upstream_id != upstream_id
        ] + new_templates
        await self._persist_upstream(upstream_id)
        return new_resources

    async def refresh_prompts_for_upstream(
        self, upstream_id: str,
    ) -> list[DiscoveredPrompt]:
        """Re-discover only prompts for one upstream — counterpart of
        ``refresh_resources_for_upstream``."""
        new_prompts = await self._discover_prompts(upstream_id)
        self._prompts = [
            p for p in self._prompts if p.upstream_id != upstream_id
        ] + new_prompts
        await self._persist_upstream(upstream_id)
        return new_prompts

    def to_mcp_tools(self, discovered: list[DiscoveredTool]) -> list[mcp_types.Tool]:
        result: list[mcp_types.Tool] = []
        for t in discovered:
            display = self.display_name_for(t.upstream_id)
            annotations: mcp_types.ToolAnnotations | None = None
            if t.annotations:
                # Prefix both title-bearing fields: clients display
                # ``title`` if present else ``annotations.title``, so a
                # prefix on only one leaves the other showing unlabelled.
                annotations = mcp_types.ToolAnnotations(
                    title=prefix_display_title(display, t.annotations.title),
                    readOnlyHint=t.annotations.readOnlyHint,
                    destructiveHint=t.annotations.destructiveHint,
                    idempotentHint=t.annotations.idempotentHint,
                    openWorldHint=t.annotations.openWorldHint,
                )
            # MCP SDK ``Tool.meta`` is aliased to ``_meta`` without
            # ``populate_by_name`` (FINDINGS §3) — passing ``meta=...``
            # to the constructor silently drops it. We use
            # ``model_validate`` with the alias key so widget URIs
            # actually round-trip to the wire. Annotations get the same
            # treatment so we keep one consistent construction shape.
            payload: dict[str, Any] = {
                "name": t.prefixed_name,
                "title": prefix_display_title(display, t.title),
                "description": t.description,
                "inputSchema": t.input_schema,
                # Don't pass outputSchema — the proxy can't guarantee
                # structured output format matching the upstream's schema.
                "annotations": (
                    annotations.model_dump(exclude_none=True)
                    if annotations is not None else None
                ),
            }
            if t.meta is not None:
                payload["_meta"] = t.meta
            result.append(mcp_types.Tool.model_validate(payload))
        return result
