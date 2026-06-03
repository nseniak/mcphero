from __future__ import annotations

import contextvars
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import mcp.types as mcp_types
import structlog
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities
from pydantic import AnyUrl

from mcpolis.domain.model.upstream import (
    DiscoveredPrompt,
    DiscoveredResource,
    DiscoveredResourceTemplate,
    UpstreamSelfDescription,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID, MULTI_ORG_SENTINEL
from mcpolis.domain.ports.organization_repository import Organization
from mcpolis.domain.services.tool_registry import (
    SEPARATOR,
    prefix_display_title,
)
from mcpolis.domain.services.tool_router import UpstreamRouterError
from mcpolis.domain.services.uri_wrapping import (
    WrappedUriError,
    unwrap_resource_uri,
    wrap_resource_uri,
    wrap_widget_uri,
)

_DEFAULT_INSTRUCTIONS = (
    "MCP Hero is a gateway that proxies multiple MCP servers through a "
    "single connection. Tools are namespaced as `{upstream}__{tool_name}` "
    "(e.g. `gmail__send_message`). Prefer these prefixed tools over "
    "same-named tools from other MCP servers in your connection list — "
    "MCP Hero audits every call and routes to configured credentials. "
    "Directly-connected MCPs the user may also have enabled are "
    "unaudited; use them only when MCP Hero doesn't expose an equivalent."
)

_MULTI_ORG_INSTRUCTIONS = (
    "MCP Hero aggregates tools from every organization you belong to. "
    "Tools are namespaced as `{org}__{upstream}__{tool_name}` "
    "(e.g. `acme__gmail__send_message`). Prefer these prefixed tools over "
    "same-named tools from other MCP servers in your connection list — "
    "MCP Hero enforces each org's access policy, audits every call, and "
    "uses the credentials each team has configured for shared use."
)


def _instructions_for_user_orgs(orgs: list[Organization]) -> str:
    """Build session instructions tailored to the user's org list.

    1 org: a personalized welcome that names the team — same energy as
    the slug-scoped admin MCP, but with the multi-org tool prefix
    documented (cloud always uses ``{org}__{upstream}__{tool}``, even
    when the user only has one org).

    N>=2 orgs: an aggregated welcome that names the orgs by display
    name so the LLM can correlate tool prefixes to teams without
    having to scan tool names first.

    0 orgs: shouldn't happen (the OAuth callback rejects users with no
    memberships) — fall through to the generic multi-org string.
    """
    if not orgs:
        return _MULTI_ORG_INSTRUCTIONS
    if len(orgs) == 1:
        org = orgs[0]
        return (
            f"MCP Hero for {org.display_name}. This gateway gives your "
            f"AI client access to {org.display_name}'s configured MCP "
            f"servers through a single connection. Tools are namespaced "
            f"as `{{org}}__{{upstream}}__{{tool_name}}` "
            f"(e.g. `{org.slug}__gmail__send_message`); the leading "
            f"`{org.slug}__` segment names the owning organization. "
            f"Prefer these prefixed tools over same-named tools from "
            f"other MCP servers in your connection list — MCP Hero "
            f"enforces {org.display_name}'s access policy, audits every "
            f"call, and uses the credentials the team has configured "
            f"for shared use."
        )
    org_list = ", ".join(o.display_name for o in orgs)
    example_slug = orgs[0].slug
    return (
        f"MCP Hero aggregates tools from every organization you belong "
        f"to: {org_list}. Tools are namespaced as "
        f"`{{org}}__{{upstream}}__{{tool_name}}` "
        f"(e.g. `{example_slug}__gmail__send_message`); the leading "
        f"segment names the owning organization. Prefer these prefixed "
        f"tools over same-named tools from other MCP servers in your "
        f"connection list — MCP Hero enforces each org's access policy, "
        f"audits every call, and uses the credentials each team has "
        f"configured for shared use."
    )


def _instructions_for_org(
    org_id: str, display_name: str | None,
) -> str:
    if org_id == MULTI_ORG_SENTINEL:
        return _MULTI_ORG_INSTRUCTIONS
    if org_id == DEFAULT_ORG_ID or not display_name:
        return _DEFAULT_INSTRUCTIONS
    return (
        f"MCP Hero for {display_name}. This gateway gives your AI client "
        f"access to {display_name}'s configured MCP servers through a "
        f"single connection. Tools are namespaced as "
        f"`{{upstream}}__{{tool_name}}` (e.g. `gmail__send_message`). "
        f"Prefer these prefixed tools over same-named tools from other "
        f"MCP servers in your connection list: MCP Hero enforces "
        f"{display_name}'s access policy, audits every call, and uses "
        f"the credentials the team has configured for shared use. "
        f"Directly-connected MCPs may use personal rather than team "
        f"credentials and are unaudited — use them only when MCP Hero "
        f"doesn't expose an equivalent."
    )

if TYPE_CHECKING:
    from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
    from mcpolis.domain.services.org_service import OrgService


def _merge_extension_value(
    base: dict[str, Any], incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge two ``ServerCapabilities.extensions`` value dicts.

    Same-key list fields (e.g. ``mimeTypes``) get unioned; same-key
    scalars take the *base* value (first-seen wins, deterministic
    across reorderings); same-key dicts recurse. Used by
    ``_aggregated_extensions`` so two upstreams advertising the same
    extension URI don't clobber each other's MIME lists.
    """
    out: dict[str, Any] = dict(base)
    for k, v in incoming.items():
        if k not in out:
            out[k] = v
            continue
        existing = out[k]
        if isinstance(existing, list) and isinstance(v, list):
            existing_list: list[Any] = list(existing)  # type: ignore[reportUnknownArgumentType]
            v_list: list[Any] = list(v)  # type: ignore[reportUnknownArgumentType]
            for item in v_list:
                if item not in existing_list:
                    existing_list.append(item)
            out[k] = existing_list
        elif isinstance(existing, dict) and isinstance(v, dict):
            out[k] = _merge_extension_value(existing, v)  # type: ignore[reportUnknownArgumentType]
        # Scalar (or type mismatch): keep the base value.
    return out


def _aggregated_extensions(
    runtimes: list[OrgRuntime],
) -> dict[str, dict[str, Any]]:
    """Union the ``capabilities.extensions`` of every upstream of every
    runtime in *runtimes*.

    Iteration is deterministic (registry order → upstream order) so the
    aggregated dict is stable across calls. Returns ``{}`` when no
    upstream advertises any extension — callers should drop the field
    entirely rather than emit ``"extensions": {}`` (the SDK round-trips
    empties as truthy keys).
    """
    aggregated: dict[str, dict[str, Any]] = {}
    for runtime in runtimes:
        for upstream_id in runtime.tool_registry.get_upstream_ids():
            sd = runtime.tool_registry.get_self_description(upstream_id)
            if sd is None:
                continue
            for ext_key, ext_val in sd.capabilities_extensions.items():
                if ext_key in aggregated:
                    aggregated[ext_key] = _merge_extension_value(
                        aggregated[ext_key], ext_val,
                    )
                else:
                    aggregated[ext_key] = dict(ext_val)
    return aggregated


def _opts_with_extensions(
    opts: InitializationOptions,
    extensions: dict[str, dict[str, Any]],
) -> InitializationOptions:
    """Return a copy of *opts* with ``extensions`` injected into
    ``capabilities``.

    ``ServerCapabilities`` has ``model_config.extra = "allow"`` but no
    typed field for extensions, so we round-trip via ``model_dump``
    (with ``by_alias=True`` to preserve any aliased fields the SDK
    adds in future) and ``model_validate``. Same trick as the POC at
    ``mcp-app-poc:server.py:381``.
    """
    caps_dict = opts.capabilities.model_dump(
        exclude_none=True, by_alias=True,
    )
    caps_dict["extensions"] = extensions
    return opts.model_copy(
        update={"capabilities": ServerCapabilities.model_validate(caps_dict)},
    )

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# MCP tool-name cap. The MCP / Anthropic Tool Use API treats tool names
# as identifiers and rejects anything longer than 64 chars. We keep
# this constant local rather than fish it out of the SDK so a future
# SDK bump doesn't silently change the slug-validation contract.
MAX_TOOL_NAME_LENGTH = 64

# Upper bound for any single upstream's ``description`` / ``instructions``
# string when folded into the gateway's own ``initialize`` instructions.
# Tunable knob — Notion's current ``serverInfo.description`` clocks in
# around 280 chars, well below the cap. Trimmed on a clean word boundary.
UPSTREAM_DESCRIPTION_TRUNCATION_CHARS = 500

# Set by middleware before MCP handlers run
current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user_id", default="anonymous"
)
current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_session_id", default=None
)
# Per-request org. In standalone mode this is always DEFAULT_ORG_ID;
# in cloud mode middleware resolves it from the URL slug for
# /admin-mcp/{slug} or sets MULTI_ORG_SENTINEL for the fixed /mcp URL.
current_org_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_org_id", default=DEFAULT_ORG_ID
)
# The human-readable slug for the current org, set by the cloud-mode
# ``OrgContextMiddleware`` alongside ``current_org_id``. Empty string
# in standalone mode, on the fixed cloud /mcp URL, or when no slug was
# present in the request. Used by slug-aware OAuth metadata to
# advertise per-org admin resource URLs.
current_org_slug: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_org_slug", default=""
)
# Pre-resolved memberships for the authenticated user, set by the
# cloud-mode user-orgs prefetch middleware before MCP handlers run.
# ``None`` outside the multi-org code path (admin MCP, standalone, or
# when no auth context was set yet). Used by ``_org_scoped_init_options``
# to pick a per-session ``instructions`` string that names the user's
# org(s) — read sync because the MCP SDK's init_options entrypoint is
# sync.
current_user_orgs: contextvars.ContextVar[list[Organization] | None] = (
    contextvars.ContextVar("current_user_orgs", default=None)
)


def _get_current_user() -> str:
    """Get user identity from OAuth context (if available) or header-based context var."""
    auth_user = auth_context_var.get(None)
    if auth_user is not None:
        return auth_user.display_name
    return current_user_id.get()


def create_mcp_server(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService | None = None,
) -> Server[Any, Any]:
    """Build the MCP gateway server.

    ``org_service`` is required for multi-org cloud-mode requests
    (``current_org_id == MULTI_ORG_SENTINEL``); without it the gateway
    can't enumerate the user's memberships. Standalone-mode tests
    that only exercise the single-org path can omit it.
    """
    server: Server[Any, Any] = Server("MCP Hero")

    # ``Server.create_initialization_options`` is called per new MCP
    # session by ``StreamableHTTPSessionManager``. ``OrgContextMiddleware``
    # has set ``current_org_id`` by then, so we can override the method
    # to return org-scoped instructions — the gateway's sole Server
    # instance serves every org, and only the instructions string varies.
    _original_init_options = server.create_initialization_options

    def _org_scoped_init_options(
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        # Declare ``tools.listChanged`` (plus the resource / prompt
        # equivalents) so clients know to listen for and act on the
        # ``notifications/{tools,resources,prompts}/list_changed``
        # messages PolicyNotifier pushes after policy / upstream changes.
        # Without these, spec-conforming clients drop the notifications
        # silently.
        if notification_options is None:
            notification_options = NotificationOptions(
                tools_changed=True,
                resources_changed=True,
                prompts_changed=True,
            )
        else:
            notification_options.tools_changed = True
            notification_options.resources_changed = True
            notification_options.prompts_changed = True
        opts = _original_init_options(
            notification_options=notification_options,
            experimental_capabilities=experimental_capabilities,
        )
        try:
            org_id = current_org_id.get()
        except LookupError:
            org_id = DEFAULT_ORG_ID
        # Aggregate extensions advertised by the org's connected upstreams
        # (or every membership runtime in multi-org mode). Re-advertised
        # so MCP-Apps clients see ``io.modelcontextprotocol/ui`` and
        # actually fetch ``ui://`` resources after ``initialize``.
        if org_id == MULTI_ORG_SENTINEL:
            user_orgs_for_ext = current_user_orgs.get() or []
            ext_runtimes = [
                runtime_manager.get_cached(o.id) for o in user_orgs_for_ext
            ]
        else:
            ext_runtimes = [runtime_manager.get_cached(org_id)]
        aggregated_ext = _aggregated_extensions(
            [r for r in ext_runtimes if r is not None],
        )
        if aggregated_ext:
            opts = _opts_with_extensions(opts, aggregated_ext)
        display_name: str | None
        if org_id == MULTI_ORG_SENTINEL:
            display_name = None
            user_orgs = current_user_orgs.get()
            if user_orgs is not None and len(user_orgs) == 1:
                # Single-org users get the org name in serverInfo.name
                # too, matching the slug-scoped admin MCP wording.
                server_name = f"MCP Hero — {user_orgs[0].display_name}"
            else:
                server_name = "MCP Hero"
            base_instructions = (
                _instructions_for_user_orgs(user_orgs)
                if user_orgs is not None
                else _MULTI_ORG_INSTRUCTIONS
            )
            instructions = (
                _instructions_with_upstreams_multi_org(
                    runtime_manager, user_orgs, base_instructions,
                )
                if user_orgs is not None
                else base_instructions
            )
            return opts.model_copy(
                update={
                    "server_name": server_name,
                    "instructions": instructions,
                }
            )
        else:
            display_name = runtime_manager.get_display_name(org_id)
            # Name differentiation: MCP clients key/display by serverInfo
            # name, so two MCP Hero instances (e.g. local-dev vs prod, or
            # two orgs in the same deployment) advertising the same literal
            # "MCP Hero" end up ambiguous in client UIs. Append the org's
            # display name when we have one.
            server_name = (
                f"MCP Hero — {display_name}"
                if display_name and org_id != DEFAULT_ORG_ID
                else "MCP Hero"
            )
        base = _instructions_for_org(org_id, display_name)
        return opts.model_copy(
            update={
                "server_name": server_name,
                "instructions": _instructions_for_org_with_upstreams(
                    runtime_manager, org_id,
                    base_instructions=base,
                    name_prefix="",
                ),
            }
        )

    server.create_initialization_options = _org_scoped_init_options

    @server.list_tools()
    async def handle_list_tools() -> list[mcp_types.Tool]:  # pyright: ignore[reportUnusedFunction]
        user_id = _get_current_user()
        org_id = current_org_id.get()

        if org_id == MULTI_ORG_SENTINEL:
            return await _list_tools_multi_org(
                runtime_manager, org_service, user_id,
            )
        return await _list_tools_single_org(
            runtime_manager, org_id, user_id,
        )

    @server.call_tool(validate_input=False)
    async def handle_call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any] | None
    ) -> Iterable[mcp_types.ContentBlock]:
        user_id = _get_current_user()
        session_id = current_session_id.get()
        org_id = current_org_id.get()

        if org_id == MULTI_ORG_SENTINEL:
            result = await _call_tool_multi_org(
                runtime_manager,
                org_service,
                user_id=user_id,
                session_id=session_id,
                prefixed_name=name,
                arguments=arguments or {},
            )
            return result.content

        result = await _call_tool_single_org(
            runtime_manager,
            org_id=org_id,
            user_id=user_id,
            session_id=session_id,
            prefixed_name=name,
            arguments=arguments or {},
        )
        return result.content

    # ── Resources ──────────────────────────────────────────────────────

    @server.list_resources()
    async def handle_list_resources() -> list[mcp_types.Resource]:  # pyright: ignore[reportUnusedFunction]
        user_id = _get_current_user()
        org_id = current_org_id.get()
        if org_id == MULTI_ORG_SENTINEL:
            return await _list_resources_multi_org(
                runtime_manager, org_service, user_id,
            )
        return _list_resources_single_org(
            runtime_manager, org_id, user_id,
        )

    @server.list_resource_templates()
    async def handle_list_resource_templates() -> list[mcp_types.ResourceTemplate]:  # pyright: ignore[reportUnusedFunction]
        user_id = _get_current_user()
        org_id = current_org_id.get()
        if org_id == MULTI_ORG_SENTINEL:
            return await _list_resource_templates_multi_org(
                runtime_manager, org_service, user_id,
            )
        return _list_resource_templates_single_org(
            runtime_manager, org_id, user_id,
        )

    @server.read_resource()
    async def handle_read_resource(  # pyright: ignore[reportUnusedFunction]
        uri: AnyUrl,
    ) -> Iterable[ReadResourceContents]:
        user_id = _get_current_user()
        session_id = current_session_id.get()
        org_id = current_org_id.get()
        if org_id == MULTI_ORG_SENTINEL:
            return await _read_resource_multi_org(
                runtime_manager, org_service,
                user_id=user_id, session_id=session_id,
                wrapped_uri=str(uri),
            )
        return await _read_resource_single_org(
            runtime_manager,
            org_id=org_id,
            user_id=user_id,
            session_id=session_id,
            wrapped_uri=str(uri),
        )

    # ── Prompts ────────────────────────────────────────────────────────

    @server.list_prompts()
    async def handle_list_prompts() -> list[mcp_types.Prompt]:  # pyright: ignore[reportUnusedFunction]
        user_id = _get_current_user()
        org_id = current_org_id.get()
        if org_id == MULTI_ORG_SENTINEL:
            return await _list_prompts_multi_org(
                runtime_manager, org_service, user_id,
            )
        return _list_prompts_single_org(
            runtime_manager, org_id, user_id,
        )

    @server.get_prompt()
    async def handle_get_prompt(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, str] | None,
    ) -> mcp_types.GetPromptResult:
        user_id = _get_current_user()
        session_id = current_session_id.get()
        org_id = current_org_id.get()
        if org_id == MULTI_ORG_SENTINEL:
            return await _get_prompt_multi_org(
                runtime_manager, org_service,
                user_id=user_id, session_id=session_id,
                prefixed_name=name, arguments=arguments,
            )
        return await _get_prompt_single_org(
            runtime_manager,
            org_id=org_id,
            user_id=user_id,
            session_id=session_id,
            prefixed_name=name,
            arguments=arguments,
        )

    return server


async def _list_tools_single_org(
    runtime_manager: OrgRuntimeManager,
    org_id: str,
    user_id: str,
) -> list[mcp_types.Tool]:
    runtime = await runtime_manager.get(org_id)
    discovered = _discovered_tools_for_user(runtime, user_id)
    tools = runtime.tool_registry.to_mcp_tools(discovered)
    org_slug = _slug_for_single_org(runtime_manager, org_id)
    _apply_widget_meta_rewrites(tools, discovered, org_slug=org_slug)
    logger.info(
        "gateway.tools.list.success",
        org_id=org_id,
        tool_count=len(tools),
    )
    return tools


async def _list_tools_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService | None,
    user_id: str,
) -> list[mcp_types.Tool]:
    if org_service is None:
        logger.error(
            "gateway.tools.list.multi_org.no_org_service",
            user_id=user_id,
        )
        return []

    orgs = await org_service.list_user_orgs(user_id)
    if not orgs:
        logger.info(
            "gateway.tools.list.multi_org.no_memberships",
            user_id=user_id,
        )
        return []

    aggregated: list[mcp_types.Tool] = []
    for org in orgs:
        runtime = await runtime_manager.get(org.id)
        discovered = _discovered_tools_for_user(runtime, user_id)
        org_tools = runtime.tool_registry.to_mcp_tools(discovered)
        # Rewrite widget URIs *before* prefixing the tool name so the
        # rewrite helper sees the raw upstream id (it bakes the slug +
        # upstream id into the wrapped resource URI itself, but uses
        # the discovered tool's plain ``upstream_id`` for the lookup).
        _apply_widget_meta_rewrites(
            org_tools, discovered, org_slug=org.slug,
        )
        # Prefix each tool with the org slug so clients can disambiguate
        # same-named upstreams across orgs.
        for tool in org_tools:
            tool.name = f"{org.slug}{SEPARATOR}{tool.name}"
        aggregated.extend(org_tools)

    logger.info(
        "gateway.tools.list.multi_org.success",
        user_id=user_id,
        org_count=len(orgs),
        tool_count=len(aggregated),
    )
    return aggregated


# ─── Widget URI rewrite ───────────────────────────────────────────────
#
# MCP-Apps tools advertise a widget HTML resource via
# ``_meta.ui.resourceUri`` (and the legacy flat ``_meta["ui/resourceUri"]``
# duplicate the ext-apps SDK normalizer emits). The URI points at a
# ``ui://...`` resource the upstream serves; clients fetch it through
# ``resources/read``. The gateway already routes ``resources/read``
# through ``unwrap_resource_uri``, so we rewrap the widget URIs the
# same way before emitting tools/list — that way the round-trip lands
# on the right upstream session and the original ``ui://`` URI is
# what reaches the server.

# Both keys are rewritten because the ext-apps server SDK
# (``ext-apps@1.7.0:src/server/index.ts:registerAppTool``) always emits
# both shapes; spec-compliant clients only consume one, but we don't
# know which, so it's cheaper to rewrite both than to discover.
_WIDGET_URI_NESTED_KEY = "ui"
_WIDGET_URI_NESTED_FIELD = "resourceUri"
_WIDGET_URI_FLAT_KEY = "ui/resourceUri"


def _rewrite_widget_meta(
    meta: dict[str, Any] | None,
    *,
    org_slug: str,
    upstream_id: str,
) -> dict[str, Any] | None:
    """Return a copy of *meta* with widget URIs rewrapped.

    Uses ``wrap_widget_uri`` so the rewritten URI keeps the
    spec-required ``ui://`` scheme (MCP Apps clients hard-validate
    that prefix; rewriting to ``mcphero://`` makes the Inspector
    crash with "Invalid UI resource URI"). Touches only the two
    known widget URI slots (modern nested ``ui.resourceUri`` and
    legacy flat ``ui/resourceUri``); arbitrary other keys pass
    through verbatim. ``meta`` is *not* mutated. Returns ``None``
    if the input was ``None`` (so callers can avoid emitting an
    empty ``_meta`` field on tools without one).
    """
    if meta is None:
        return None
    new_meta: dict[str, Any] = dict(meta)
    nested = new_meta.get(_WIDGET_URI_NESTED_KEY)
    if isinstance(nested, dict):
        nested_copy: dict[str, Any] = dict(nested)  # type: ignore[reportUnknownArgumentType]
        raw = nested_copy.get(_WIDGET_URI_NESTED_FIELD)
        if isinstance(raw, str) and raw:
            nested_copy[_WIDGET_URI_NESTED_FIELD] = wrap_widget_uri(
                org_slug=org_slug,
                upstream_id=upstream_id,
                original_uri=raw,
            )
            new_meta[_WIDGET_URI_NESTED_KEY] = nested_copy
    flat = new_meta.get(_WIDGET_URI_FLAT_KEY)
    if isinstance(flat, str) and flat:
        new_meta[_WIDGET_URI_FLAT_KEY] = wrap_widget_uri(
            org_slug=org_slug,
            upstream_id=upstream_id,
            original_uri=flat,
        )
    return new_meta


def _apply_widget_meta_rewrites(
    tools: list[mcp_types.Tool],
    discovered: list[Any],
    *,
    org_slug: str,
) -> None:
    """In-place: rewrap widget URIs on each emitted tool's ``_meta``.

    Pairs each ``mcp_types.Tool`` with its source ``DiscoveredTool`` by
    list index — ``to_mcp_tools`` preserves order, so this is safe.
    Tools whose meta has no widget URI are left untouched.
    """
    for tool, disc in zip(tools, discovered, strict=True):
        if disc.meta is None:
            continue
        rewritten = _rewrite_widget_meta(
            disc.meta, org_slug=org_slug, upstream_id=disc.upstream_id,
        )
        tool.meta = rewritten


def _discovered_tools_for_user(runtime: OrgRuntime, user_id: str) -> list[Any]:
    """Apply policy filtering to a runtime's discovered tools."""
    tool_registry = runtime.tool_registry
    policy_engine = runtime.policy_engine

    enabled_ids = tool_registry.get_upstream_ids()
    allowed = policy_engine.get_allowed_upstreams(user_id)
    if allowed is not None:
        enabled_ids = [uid for uid in enabled_ids if uid in allowed]

    discovered = tool_registry.get_tools_for_upstreams(enabled_ids)

    if not policy_engine.is_empty:
        tool_triples = [
            (
                t.upstream_id,
                t.original_name,
                t.annotations.to_flags() if t.annotations else {},
            )
            for t in discovered
        ]
        allowed_set = {
            (uid, name)
            for uid, name, _ in policy_engine.filter_tools(user_id, tool_triples)
        }
        discovered = [
            t for t in discovered
            if (t.upstream_id, t.original_name) in allowed_set
        ]
    return discovered


async def _call_tool_single_org(
    runtime_manager: OrgRuntimeManager,
    *,
    org_id: str,
    user_id: str,
    session_id: str | None,
    prefixed_name: str,
    arguments: dict[str, Any],
) -> mcp_types.CallToolResult:
    runtime = await runtime_manager.get(org_id)
    logger.info(
        "gateway.tool.call.received", tool_name=prefixed_name, org_id=org_id,
    )
    # Bare-name fallback: MCP-Apps widgets call ``app.callServerTool({
    # name: "foo"})`` with the tool's *upstream-original* name (the
    # name the upstream registered with). The host forwards that as
    # ``tools/call name="foo"`` to the gateway; without a prefix the
    # gateway can't route. Resolve unique-match by searching the
    # user's enabled upstreams and rewrite to the prefixed name.
    if SEPARATOR not in prefixed_name:
        resolved = _resolve_bare_tool_name(
            runtime, user_id=user_id, original_name=prefixed_name,
        )
        if isinstance(resolved, str):
            logger.info(
                "gateway.tool.call.bare_name_resolved",
                bare_name=prefixed_name, resolved=resolved,
            )
            prefixed_name = resolved
        else:
            return resolved  # CallToolResult error from the resolver.
    denial = _check_upstream_and_tool_policy(
        runtime, user_id=user_id, prefixed_name=prefixed_name,
        arguments=arguments,
    )
    if denial is not None:
        return denial
    return await runtime.tool_router.route_call(
        org_id=org_id,
        prefixed_name=prefixed_name,
        arguments=arguments,
        user_id=user_id,
        session_id=session_id,
    )


def _resolve_bare_tool_name(
    runtime: OrgRuntime,
    *,
    user_id: str,
    original_name: str,
) -> str | mcp_types.CallToolResult:
    """Find the unique enabled upstream that exposes a tool with the
    given *original_name* and return its prefixed form.

    Returns either the resolved ``{upstream_id}__{original_name}``
    string OR a ``CallToolResult`` error to surface (not-found,
    ambiguous). This is the gateway's fallback for tool calls coming
    from MCP-Apps widgets, which don't know they're being proxied
    and therefore call by bare name.
    """
    enabled_ids = _allowed_upstream_ids(runtime, user_id)
    matches: list[str] = []
    for t in runtime.tool_registry.get_tools_for_upstreams(enabled_ids):
        if t.original_name == original_name:
            matches.append(t.upstream_id)
    if not matches:
        return _access_denied(
            f"Unknown tool '{original_name}'. Tools must be called by their "
            f"prefixed name (``{{upstream}}__{{tool}}``); only widget-internal "
            f"calls may use bare names, and only when one upstream owns the name."
        )
    if len(matches) > 1:
        return _access_denied(
            f"Ambiguous tool name '{original_name}': owned by multiple "
            f"upstreams ({', '.join(sorted(matches))}). Call by prefixed name."
        )
    return f"{matches[0]}{SEPARATOR}{original_name}"


async def _call_tool_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService | None,
    *,
    user_id: str,
    session_id: str | None,
    prefixed_name: str,
    arguments: dict[str, Any],
) -> mcp_types.CallToolResult:
    if org_service is None:
        return _access_denied(
            f"Multi-org gateway is misconfigured: org_service unavailable.",
        )

    parts = prefixed_name.split(SEPARATOR, 2)
    if len(parts) < 3:
        # Bare-name fallback for MCP-Apps widget callbacks (see
        # ``_resolve_bare_tool_name`` for the rationale). Search every
        # org the user belongs to for a unique tool match; rewrite to
        # the full ``{org}__{upstream}__{tool}`` form before continuing.
        if SEPARATOR not in prefixed_name:
            resolved = await _resolve_bare_tool_name_multi_org(
                runtime_manager, org_service,
                user_id=user_id, original_name=prefixed_name,
            )
            if isinstance(resolved, str):
                logger.info(
                    "gateway.tool.call.multi_org.bare_name_resolved",
                    bare_name=prefixed_name, resolved=resolved,
                )
                prefixed_name = resolved
                parts = prefixed_name.split(SEPARATOR, 2)
            else:
                return resolved
        if len(parts) < 3:
            return _access_denied(
                f"Tool '{prefixed_name}' is missing the org prefix expected "
                f"on the multi-org gateway: "
                f"'{{org}}__{{upstream}}__{{tool}}'.",
            )
    org_slug, _, inner_prefixed = prefixed_name.partition(SEPARATOR)

    # Defense in depth: confirm the user is actually a member of the
    # org they're trying to invoke. The list_tools handler only ever
    # advertises slugs the user belongs to, but a hostile client could
    # synthesize any slug + tool name pair.
    user_orgs = await org_service.list_user_orgs(user_id)
    org = next((o for o in user_orgs if o.slug == org_slug), None)
    if org is None:
        logger.warning(
            "gateway.tool.call.multi_org.not_a_member",
            user_id=user_id,
            org_slug=org_slug,
        )
        return _access_denied(
            f"Access denied: user '{user_id}' is not a member of org "
            f"'{org_slug}'.",
        )

    runtime = await runtime_manager.get(org.id)
    logger.info(
        "gateway.tool.call.multi_org.received",
        tool_name=prefixed_name,
        org_id=org.id,
        inner_name=inner_prefixed,
    )
    denial = _check_upstream_and_tool_policy(
        runtime, user_id=user_id, prefixed_name=inner_prefixed,
        arguments=arguments,
    )
    if denial is not None:
        return denial
    return await runtime.tool_router.route_call(
        org_id=org.id,
        prefixed_name=inner_prefixed,
        arguments=arguments,
        user_id=user_id,
        session_id=session_id,
    )


async def _resolve_bare_tool_name_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService,
    *,
    user_id: str,
    original_name: str,
) -> str | mcp_types.CallToolResult:
    """Multi-org variant of ``_resolve_bare_tool_name``.

    Searches every org the user belongs to for a tool with the given
    *original_name*. If exactly one ``(org, upstream, tool)`` triple
    matches, returns the prefixed form ``{org_slug}__{upstream_id}__
    {original_name}``. Returns a ``CallToolResult`` error otherwise.
    """
    user_orgs = await org_service.list_user_orgs(user_id)
    matches: list[tuple[str, str]] = []  # (org_slug, upstream_id)
    for org in user_orgs:
        runtime = runtime_manager.get_cached(org.id)
        if runtime is None:
            continue
        enabled_ids = _allowed_upstream_ids(runtime, user_id)
        for t in runtime.tool_registry.get_tools_for_upstreams(enabled_ids):
            if t.original_name == original_name:
                matches.append((org.slug, t.upstream_id))
    if not matches:
        return _access_denied(
            f"Unknown tool '{original_name}'. Tools must be called by their "
            f"prefixed name (``{{org}}__{{upstream}}__{{tool}}``); only "
            f"widget-internal calls may use bare names, and only when one "
            f"upstream across your orgs owns the name."
        )
    if len(matches) > 1:
        details = ", ".join(
            f"{org}__{up}" for org, up in sorted(matches)
        )
        return _access_denied(
            f"Ambiguous tool name '{original_name}': owned by multiple "
            f"upstreams ({details}). Call by prefixed name."
        )
    org_slug, upstream_id = matches[0]
    return (
        f"{org_slug}{SEPARATOR}{upstream_id}{SEPARATOR}{original_name}"
    )


def _check_upstream_and_tool_policy(
    runtime: OrgRuntime,
    *,
    user_id: str,
    prefixed_name: str,
    arguments: dict[str, Any],
) -> mcp_types.CallToolResult | None:
    """Returns an access-denied result, or None if the call is allowed."""
    tool_registry = runtime.tool_registry
    policy_engine = runtime.policy_engine

    parts = prefixed_name.split(SEPARATOR, 1)
    if len(parts) != 2:
        return None

    target_upstream, tool_name = parts

    all_ids = tool_registry.get_upstream_ids()
    allowed = policy_engine.get_allowed_upstreams(user_id)
    enabled_ids = (
        all_ids if allowed is None else [uid for uid in all_ids if uid in allowed]
    )

    if target_upstream not in enabled_ids:
        return _access_denied(
            f"Access denied: MCP '{target_upstream}' is disabled for "
            f"user '{user_id}'."
        )

    tool_annotations = tool_registry.get_tool_annotations(
        target_upstream, tool_name,
    )
    decision = policy_engine.decide_tool_call(
        user_id, target_upstream, tool_name, arguments,
        tool_annotations=tool_annotations,
    )
    if not decision.allowed:
        return _access_denied(f"Access denied: {decision.reason}")
    return None


def _access_denied(message: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=message)],
        isError=True,
    )


# ─── Resources ────────────────────────────────────────────────────────────


def _allowed_upstream_ids(runtime: OrgRuntime, user_id: str) -> list[str]:
    """Apply policy filtering to the list of upstream IDs for *user_id*.

    Mirrors the front half of ``_discovered_tools_for_user``. Resources
    and prompts use the same gating: an upstream the user can't reach
    via tools shouldn't expose its resources / prompts either.
    """
    enabled_ids = runtime.tool_registry.get_upstream_ids()
    allowed = runtime.policy_engine.get_allowed_upstreams(user_id)
    if allowed is not None:
        enabled_ids = [uid for uid in enabled_ids if uid in allowed]
    return enabled_ids


def _build_resource(
    *,
    discovered: DiscoveredResource,
    org_slug: str,
    name_prefix: str,
    display_name: str,
) -> mcp_types.Resource:
    """Convert a registry record into the wire ``Resource`` shape.

    *org_slug* is baked into the wrapped URI; *name_prefix* is prepended
    to the upstream's ``name`` so MCP clients see a unique identifier
    per ``{slug}__{upstream}__{name}``. *display_name* is folded into the
    human ``title`` so same-named resources from different upstreams stay
    distinguishable. The wrapped URI carries the routing information back
    to ``read_resource``. ``_meta`` from the upstream (CSP hints,
    alternative MIMEs, etc.) is forwarded verbatim — only tool meta
    carries gateway-routable widget URIs.
    """
    wrapped = wrap_resource_uri(
        org_slug=org_slug,
        upstream_id=discovered.upstream_id,
        original_uri=discovered.original_uri,
    )
    payload: dict[str, Any] = {
        "uri": AnyUrl(wrapped),
        "name": f"{name_prefix}{discovered.name}",
        "title": prefix_display_title(display_name, discovered.title),
        "description": discovered.description,
        "mimeType": discovered.mime_type,
    }
    if discovered.meta is not None:
        payload["_meta"] = discovered.meta
    return mcp_types.Resource.model_validate(payload)


def _build_resource_template(
    *,
    discovered: DiscoveredResourceTemplate,
    org_slug: str,
    name_prefix: str,
    display_name: str,
) -> mcp_types.ResourceTemplate:
    wrapped = wrap_resource_uri(
        org_slug=org_slug,
        upstream_id=discovered.upstream_id,
        original_uri=discovered.original_uri_template,
        is_template=True,
    )
    payload: dict[str, Any] = {
        "uriTemplate": wrapped,
        "name": f"{name_prefix}{discovered.name}",
        "title": prefix_display_title(display_name, discovered.title),
        "description": discovered.description,
        "mimeType": discovered.mime_type,
    }
    if discovered.meta is not None:
        payload["_meta"] = discovered.meta
    return mcp_types.ResourceTemplate.model_validate(payload)


def _build_prompt(
    *,
    discovered: DiscoveredPrompt,
    name_prefix: str,
    display_name: str,
) -> mcp_types.Prompt:
    args: list[mcp_types.PromptArgument] | None = None
    if discovered.arguments:
        args = [
            mcp_types.PromptArgument(
                name=a.name, description=a.description, required=a.required,
            )
            for a in discovered.arguments
        ]
    payload: dict[str, Any] = {
        "name": f"{name_prefix}{discovered.original_name}",
        "title": prefix_display_title(display_name, discovered.title),
        "description": discovered.description,
        "arguments": (
            [a.model_dump(exclude_none=True) for a in args]
            if args is not None else None
        ),
    }
    if discovered.meta is not None:
        payload["_meta"] = discovered.meta
    return mcp_types.Prompt.model_validate(payload)


def _slug_for_single_org(
    runtime_manager: OrgRuntimeManager, org_id: str,
) -> str:
    """Slug to bake into the wrapped URI for the single-org code path.

    Standalone-mode default org and any cached org slug both flow through
    here. Falls back to ``org_id`` itself when no slug is registered —
    keeps the wrapped URI well-formed even on test paths that build
    runtimes through the factory rather than the lifespan.
    """
    return runtime_manager.get_slug(org_id) or org_id


def _list_resources_single_org(
    runtime_manager: OrgRuntimeManager,
    org_id: str,
    user_id: str,
) -> list[mcp_types.Resource]:
    runtime = runtime_manager.get_cached(org_id)
    if runtime is None:
        return []
    enabled_ids = _allowed_upstream_ids(runtime, user_id)
    discovered = runtime.tool_registry.get_resources_for_upstreams(enabled_ids)
    org_slug = _slug_for_single_org(runtime_manager, org_id)
    return [
        _build_resource(
            discovered=r, org_slug=org_slug,
            name_prefix=f"{r.upstream_id}{SEPARATOR}",
            display_name=runtime.tool_registry.display_name_for(r.upstream_id),
        )
        for r in discovered
    ]


async def _list_resources_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService | None,
    user_id: str,
) -> list[mcp_types.Resource]:
    if org_service is None:
        logger.error(
            "gateway.resources.list.multi_org.no_org_service",
            user_id=user_id,
        )
        return []
    orgs = await org_service.list_user_orgs(user_id)
    if not orgs:
        return []
    aggregated: list[mcp_types.Resource] = []
    for org in orgs:
        runtime = await runtime_manager.get(org.id)
        enabled_ids = _allowed_upstream_ids(runtime, user_id)
        discovered = runtime.tool_registry.get_resources_for_upstreams(
            enabled_ids,
        )
        for r in discovered:
            aggregated.append(
                _build_resource(
                    discovered=r,
                    org_slug=org.slug,
                    name_prefix=(
                        f"{org.slug}{SEPARATOR}{r.upstream_id}{SEPARATOR}"
                    ),
                    display_name=runtime.tool_registry.display_name_for(
                        r.upstream_id,
                    ),
                )
            )
    return aggregated


def _list_resource_templates_single_org(
    runtime_manager: OrgRuntimeManager,
    org_id: str,
    user_id: str,
) -> list[mcp_types.ResourceTemplate]:
    runtime = runtime_manager.get_cached(org_id)
    if runtime is None:
        return []
    enabled_ids = _allowed_upstream_ids(runtime, user_id)
    discovered = runtime.tool_registry.get_resource_templates_for_upstreams(
        enabled_ids,
    )
    org_slug = _slug_for_single_org(runtime_manager, org_id)
    return [
        _build_resource_template(
            discovered=t, org_slug=org_slug,
            name_prefix=f"{t.upstream_id}{SEPARATOR}",
            display_name=runtime.tool_registry.display_name_for(t.upstream_id),
        )
        for t in discovered
    ]


async def _list_resource_templates_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService | None,
    user_id: str,
) -> list[mcp_types.ResourceTemplate]:
    if org_service is None:
        return []
    orgs = await org_service.list_user_orgs(user_id)
    if not orgs:
        return []
    aggregated: list[mcp_types.ResourceTemplate] = []
    for org in orgs:
        runtime = await runtime_manager.get(org.id)
        enabled_ids = _allowed_upstream_ids(runtime, user_id)
        discovered = (
            runtime.tool_registry.get_resource_templates_for_upstreams(
                enabled_ids,
            )
        )
        for t in discovered:
            aggregated.append(
                _build_resource_template(
                    discovered=t,
                    org_slug=org.slug,
                    name_prefix=(
                        f"{org.slug}{SEPARATOR}{t.upstream_id}{SEPARATOR}"
                    ),
                    display_name=runtime.tool_registry.display_name_for(
                        t.upstream_id,
                    ),
                )
            )
    return aggregated


def _read_resource_error(message: str) -> list[ReadResourceContents]:
    """User-facing error → text content the SDK wraps as TextResourceContents.

    ``read_resource`` has no ``isError`` bit, so we can't surface a
    "this failed" flag the way ``call_tool`` does. The clearest signal
    is a single text item carrying the message — clients display it
    inline next to whichever resource was requested.
    """
    return [ReadResourceContents(content=message, mime_type="text/plain")]


def _result_contents_to_iterable(
    result: mcp_types.ReadResourceResult,
) -> Iterable[ReadResourceContents]:
    """Adapt an upstream's ``ReadResourceResult`` for the SDK decorator.

    The SDK's ``@server.read_resource()`` decorator forces the response
    URI to match the request URI (the *wrapped* one), so we forward
    only the content + mime type + ``_meta`` and let the decorator
    re-attach the URI. ``_meta`` carries widget CSP hints
    (``ui.csp.resourceDomains``, etc.) — dropping it silently breaks
    MCP-Apps widgets that load any external script.
    """
    out: list[ReadResourceContents] = []
    for c in result.contents:
        if isinstance(c, mcp_types.TextResourceContents):
            out.append(
                ReadResourceContents(
                    content=c.text, mime_type=c.mimeType, meta=c.meta,
                )
            )
        else:
            # blob: forward the bytes payload (already decoded by the
            # SDK on the read side once we hand it bytes).
            import base64
            out.append(
                ReadResourceContents(
                    content=base64.b64decode(c.blob),
                    mime_type=c.mimeType,
                    meta=c.meta,
                )
            )
    return out


async def _read_resource_single_org(
    runtime_manager: OrgRuntimeManager,
    *,
    org_id: str,
    user_id: str,
    session_id: str | None,
    wrapped_uri: str,
) -> Iterable[ReadResourceContents]:
    runtime = await runtime_manager.get(org_id)
    try:
        wrapped = unwrap_resource_uri(wrapped_uri)
        target_upstream_id = wrapped.upstream_id
        target_original_uri = wrapped.original_uri
    except WrappedUriError:
        # Bare-URI fallback: widget JS may call ``app.readServerResource({
        # uri: "ui://widget/data/foo"})`` with the upstream-native URI
        # (no idea it's being proxied). Resolve unique-match by
        # searching the user's enabled upstreams' registries.
        resolved = _resolve_bare_resource_uri(
            runtime, user_id=user_id, original_uri=wrapped_uri,
        )
        if isinstance(resolved, tuple):
            target_upstream_id, target_original_uri = resolved
            logger.info(
                "gateway.resources.read.bare_uri_resolved",
                bare_uri=wrapped_uri, upstream_id=target_upstream_id,
            )
        else:
            return resolved
    if target_upstream_id not in runtime.tool_registry.get_upstream_ids():
        return _read_resource_error(
            f"Unknown upstream '{target_upstream_id}' for resource."
        )
    if target_upstream_id not in _allowed_upstream_ids(runtime, user_id):
        return _read_resource_error(
            f"Access denied: MCP '{target_upstream_id}' is disabled for "
            f"user '{user_id}'."
        )
    try:
        result = await runtime.tool_router.read_resource(
            org_id=org_id,
            upstream_id=target_upstream_id,
            original_uri=target_original_uri,
            user_id=user_id,
            session_id=session_id,
        )
    except UpstreamRouterError as exc:
        return _read_resource_error(exc.message)
    return _result_contents_to_iterable(result)


def _resolve_bare_resource_uri(
    runtime: OrgRuntime,
    *,
    user_id: str,
    original_uri: str,
) -> tuple[str, str] | list[ReadResourceContents]:
    """Find the unique enabled upstream that exposes a resource with
    the given *original_uri* (or a templated URI that matches).

    Returns either ``(upstream_id, original_uri)`` for routing, or a
    ``ReadResourceContents``-shaped error iterable for the
    ``resources/read`` decorator. Mirrors
    ``_resolve_bare_tool_name``'s shape — the gateway's fallback for
    widget callbacks that read resources by upstream-native URI.
    """
    enabled_ids = _allowed_upstream_ids(runtime, user_id)
    matches: list[str] = []
    for r in runtime.tool_registry.get_resources_for_upstreams(enabled_ids):
        if r.original_uri == original_uri:
            matches.append(r.upstream_id)
    if not matches:
        return _read_resource_error(
            f"Invalid resource URI: {original_uri!r}. Resources must be read "
            f"by their gateway-wrapped URI (``mcphero://...`` or the "
            f"widget form ``ui://mcphero/...``); only widget-internal "
            f"calls may use upstream-native URIs, and only when one upstream "
            f"owns the URI.",
        )
    if len(matches) > 1:
        return _read_resource_error(
            f"Ambiguous resource URI {original_uri!r}: owned by multiple "
            f"upstreams ({', '.join(sorted(matches))}). Read by wrapped URI."
        )
    return (matches[0], original_uri)


async def _read_resource_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService | None,
    *,
    user_id: str,
    session_id: str | None,
    wrapped_uri: str,
) -> Iterable[ReadResourceContents]:
    if org_service is None:
        return _read_resource_error(
            "Multi-org gateway is misconfigured: org_service unavailable."
        )
    target_org_id: str
    target_org_slug: str
    target_upstream_id: str
    target_original_uri: str
    try:
        wrapped = unwrap_resource_uri(wrapped_uri)
        target_org_slug = wrapped.org_slug
        target_upstream_id = wrapped.upstream_id
        target_original_uri = wrapped.original_uri
        user_orgs = await org_service.list_user_orgs(user_id)
        org = next(
            (o for o in user_orgs if o.slug == target_org_slug), None,
        )
        if org is None:
            logger.warning(
                "gateway.resources.read.multi_org.not_a_member",
                user_id=user_id, org_slug=target_org_slug,
            )
            return _read_resource_error(
                f"Access denied: user '{user_id}' is not a member of org "
                f"'{target_org_slug}'."
            )
        target_org_id = org.id
    except WrappedUriError:
        # Bare-URI fallback for widget callbacks. Same rationale as
        # ``_resolve_bare_resource_uri``; here we search across every
        # org the user belongs to.
        resolved = await _resolve_bare_resource_uri_multi_org(
            runtime_manager, org_service,
            user_id=user_id, original_uri=wrapped_uri,
        )
        if not isinstance(resolved, tuple):
            return resolved
        target_org_id, target_org_slug, target_upstream_id, target_original_uri = resolved
        logger.info(
            "gateway.resources.read.multi_org.bare_uri_resolved",
            bare_uri=wrapped_uri,
            org_slug=target_org_slug,
            upstream_id=target_upstream_id,
        )
    runtime = await runtime_manager.get(target_org_id)
    if target_upstream_id not in runtime.tool_registry.get_upstream_ids():
        return _read_resource_error(
            f"Unknown upstream '{target_upstream_id}' for resource."
        )
    if target_upstream_id not in _allowed_upstream_ids(runtime, user_id):
        return _read_resource_error(
            f"Access denied: MCP '{target_upstream_id}' is disabled for "
            f"user '{user_id}'."
        )
    try:
        result = await runtime.tool_router.read_resource(
            org_id=target_org_id,
            upstream_id=target_upstream_id,
            original_uri=target_original_uri,
            user_id=user_id,
            session_id=session_id,
        )
    except UpstreamRouterError as exc:
        return _read_resource_error(exc.message)
    return _result_contents_to_iterable(result)


async def _resolve_bare_resource_uri_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService,
    *,
    user_id: str,
    original_uri: str,
) -> tuple[str, str, str, str] | list[ReadResourceContents]:
    """Multi-org variant of ``_resolve_bare_resource_uri``.

    Returns ``(org_id, org_slug, upstream_id, original_uri)`` for
    routing, or a ``ReadResourceContents``-shaped error iterable.
    """
    user_orgs = await org_service.list_user_orgs(user_id)
    matches: list[tuple[str, str, str]] = []  # (org_id, org_slug, upstream_id)
    for org in user_orgs:
        runtime = runtime_manager.get_cached(org.id)
        if runtime is None:
            continue
        enabled_ids = _allowed_upstream_ids(runtime, user_id)
        for r in runtime.tool_registry.get_resources_for_upstreams(enabled_ids):
            if r.original_uri == original_uri:
                matches.append((org.id, org.slug, r.upstream_id))
    if not matches:
        return _read_resource_error(
            f"Invalid resource URI: {original_uri!r}. Resources must be read "
            f"by their gateway-wrapped URI; only widget-internal calls may "
            f"use upstream-native URIs, and only when one upstream across "
            f"your orgs owns the URI."
        )
    if len(matches) > 1:
        details = ", ".join(
            f"{slug}__{up}" for _, slug, up in sorted(matches)
        )
        return _read_resource_error(
            f"Ambiguous resource URI {original_uri!r}: owned by multiple "
            f"upstreams ({details}). Read by wrapped URI."
        )
    org_id, org_slug, upstream_id = matches[0]
    return (org_id, org_slug, upstream_id, original_uri)


# ─── Prompts ──────────────────────────────────────────────────────────────


def _list_prompts_single_org(
    runtime_manager: OrgRuntimeManager,
    org_id: str,
    user_id: str,
) -> list[mcp_types.Prompt]:
    runtime = runtime_manager.get_cached(org_id)
    if runtime is None:
        return []
    enabled_ids = _allowed_upstream_ids(runtime, user_id)
    discovered = runtime.tool_registry.get_prompts_for_upstreams(enabled_ids)
    return [
        _build_prompt(
            discovered=p,
            name_prefix=f"{p.upstream_id}{SEPARATOR}",
            display_name=runtime.tool_registry.display_name_for(p.upstream_id),
        )
        for p in discovered
    ]


async def _list_prompts_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService | None,
    user_id: str,
) -> list[mcp_types.Prompt]:
    if org_service is None:
        return []
    orgs = await org_service.list_user_orgs(user_id)
    if not orgs:
        return []
    aggregated: list[mcp_types.Prompt] = []
    for org in orgs:
        runtime = await runtime_manager.get(org.id)
        enabled_ids = _allowed_upstream_ids(runtime, user_id)
        discovered = runtime.tool_registry.get_prompts_for_upstreams(
            enabled_ids,
        )
        for p in discovered:
            aggregated.append(
                _build_prompt(
                    discovered=p,
                    name_prefix=(
                        f"{org.slug}{SEPARATOR}{p.upstream_id}{SEPARATOR}"
                    ),
                    display_name=runtime.tool_registry.display_name_for(
                        p.upstream_id,
                    ),
                )
            )
    return aggregated


def _prompt_error_result(message: str) -> mcp_types.GetPromptResult:
    """Return a single user-message ``GetPromptResult`` carrying *message*.

    ``GetPromptResult`` lacks an ``isError`` flag so the only way to
    surface a problem is through the rendered messages — match
    ``read_resource`` and put a single text turn in.
    """
    return mcp_types.GetPromptResult(
        description=None,
        messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(type="text", text=message),
            ),
        ],
    )


async def _get_prompt_single_org(
    runtime_manager: OrgRuntimeManager,
    *,
    org_id: str,
    user_id: str,
    session_id: str | None,
    prefixed_name: str,
    arguments: dict[str, str] | None,
) -> mcp_types.GetPromptResult:
    runtime = await runtime_manager.get(org_id)
    parts = prefixed_name.split(SEPARATOR, 1)
    if len(parts) != 2:
        return _prompt_error_result(f"Unknown prompt: {prefixed_name}")
    upstream_id, original_name = parts
    if upstream_id not in runtime.tool_registry.get_upstream_ids():
        return _prompt_error_result(
            f"Unknown upstream '{upstream_id}' for prompt."
        )
    if upstream_id not in _allowed_upstream_ids(runtime, user_id):
        return _prompt_error_result(
            f"Access denied: MCP '{upstream_id}' is disabled for "
            f"user '{user_id}'."
        )
    try:
        return await runtime.tool_router.get_prompt(
            org_id=org_id,
            upstream_id=upstream_id,
            original_name=original_name,
            arguments=arguments,
            user_id=user_id,
            session_id=session_id,
        )
    except UpstreamRouterError as exc:
        return _prompt_error_result(exc.message)


async def _get_prompt_multi_org(
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService | None,
    *,
    user_id: str,
    session_id: str | None,
    prefixed_name: str,
    arguments: dict[str, str] | None,
) -> mcp_types.GetPromptResult:
    if org_service is None:
        return _prompt_error_result(
            "Multi-org gateway is misconfigured: org_service unavailable."
        )
    parts = prefixed_name.split(SEPARATOR, 2)
    if len(parts) < 3:
        return _prompt_error_result(
            f"Prompt '{prefixed_name}' is missing the org prefix expected "
            f"on the multi-org gateway: '{{org}}__{{upstream}}__{{prompt}}'.",
        )
    org_slug, _, inner = prefixed_name.partition(SEPARATOR)
    user_orgs = await org_service.list_user_orgs(user_id)
    org = next((o for o in user_orgs if o.slug == org_slug), None)
    if org is None:
        logger.warning(
            "gateway.prompt.get.multi_org.not_a_member",
            user_id=user_id, org_slug=org_slug,
        )
        return _prompt_error_result(
            f"Access denied: user '{user_id}' is not a member of org "
            f"'{org_slug}'.",
        )
    inner_parts = inner.split(SEPARATOR, 1)
    if len(inner_parts) != 2:
        return _prompt_error_result(f"Unknown prompt: {prefixed_name}")
    upstream_id, original_name = inner_parts
    runtime = await runtime_manager.get(org.id)
    if upstream_id not in runtime.tool_registry.get_upstream_ids():
        return _prompt_error_result(
            f"Unknown upstream '{upstream_id}' for prompt."
        )
    if upstream_id not in _allowed_upstream_ids(runtime, user_id):
        return _prompt_error_result(
            f"Access denied: MCP '{upstream_id}' is disabled for "
            f"user '{user_id}'."
        )
    try:
        return await runtime.tool_router.get_prompt(
            org_id=org.id,
            upstream_id=upstream_id,
            original_name=original_name,
            arguments=arguments,
            user_id=user_id,
            session_id=session_id,
        )
    except UpstreamRouterError as exc:
        return _prompt_error_result(exc.message)


# ─── Self-description aggregation ─────────────────────────────────────────


def _truncate_at_word_boundary(text: str, cap: int) -> str:
    """Trim *text* to ≤ *cap* chars, preferring a clean word boundary."""
    if len(text) <= cap:
        return text
    cut = text.rfind(" ", 0, cap)
    # Prefer a word boundary if it doesn't lose too much; fall back to
    # a hard cut otherwise.
    if cut > cap // 2:
        return text[:cut].rstrip() + "…"
    return text[:cap].rstrip() + "…"


def _self_description_line(
    *,
    name_prefix: str,
    upstream_id: str,
    display_name: str,
    self_description: UpstreamSelfDescription,
) -> str | None:
    """Render a one-line summary of an upstream's self-description.

    Returns ``None`` when the upstream advertises no ``description`` /
    ``instructions`` text — the caller filters them out so the
    "Connected upstreams" block doesn't show empty bullets.
    """
    blurb = self_description.description or self_description.instructions
    if not blurb:
        return None
    blurb = _truncate_at_word_boundary(
        blurb.strip(),
        UPSTREAM_DESCRIPTION_TRUNCATION_CHARS,
    )
    return (
        f"- {name_prefix}{upstream_id} ({display_name}): {blurb}"
    )


def _instructions_for_org_with_upstreams(
    runtime_manager: OrgRuntimeManager,
    org_id: str,
    *,
    base_instructions: str,
    name_prefix: str,
) -> str:
    """Append a 'Connected upstreams' block to *base_instructions* for
    org *org_id*.

    Iterates the registry (live add/remove tracking) rather than
    ``runtime.upstreams`` (frozen at construction time) so upstreams
    added after startup still surface their descriptions. Lines are
    emitted only for upstreams that advertised a ``description`` or
    ``instructions`` string at ``initialize``.
    """
    runtime = runtime_manager.get_cached(org_id)
    if runtime is None:
        return base_instructions
    lines: list[str] = []
    for upstream_id in runtime.tool_registry.get_upstream_ids():
        sd = runtime.tool_registry.get_self_description(upstream_id)
        if sd is None:
            continue
        u_def = runtime.tool_registry.get_upstream_definition(upstream_id)
        display_name = u_def.display_name if u_def is not None else upstream_id
        line = _self_description_line(
            name_prefix=name_prefix,
            upstream_id=upstream_id,
            display_name=display_name,
            self_description=sd,
        )
        if line is not None:
            lines.append(line)
    if not lines:
        return base_instructions
    return f"{base_instructions}\n\nConnected upstreams:\n" + "\n".join(lines)


def _instructions_with_upstreams_multi_org(
    runtime_manager: OrgRuntimeManager,
    user_orgs: list[Organization],
    base_instructions: str,
) -> str:
    """Multi-org variant — group upstream descriptions by org slug."""
    blocks: list[str] = []
    for org in user_orgs:
        runtime = runtime_manager.get_cached(org.id)
        if runtime is None:
            continue
        org_lines: list[str] = []
        for upstream_id in runtime.tool_registry.get_upstream_ids():
            sd = runtime.tool_registry.get_self_description(upstream_id)
            if sd is None:
                continue
            u_def = runtime.tool_registry.get_upstream_definition(upstream_id)
            display_name = (
                u_def.display_name if u_def is not None else upstream_id
            )
            line = _self_description_line(
                name_prefix=f"{org.slug}{SEPARATOR}",
                upstream_id=upstream_id,
                display_name=display_name,
                self_description=sd,
            )
            if line is not None:
                org_lines.append(line)
        if org_lines:
            blocks.append("\n".join(org_lines))
    if not blocks:
        return base_instructions
    return (
        f"{base_instructions}\n\nConnected upstreams:\n"
        + "\n".join(blocks)
    )
