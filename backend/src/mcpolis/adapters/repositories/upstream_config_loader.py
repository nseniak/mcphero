"""Load upstream definitions by merging mcp.json (connections) + config.json (options)."""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import structlog
from pydantic import BaseModel

from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import OAuthAppsConfig
from mcpolis.domain.services.oauth_app_resolver import resolve_oauth_app
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def auto_detect_auth_mode(server_config: dict[str, Any]) -> AuthMode:
    """Detect auth mode from a standard mcpServers entry."""
    if "command" in server_config:
        return AuthMode.service_account
    headers = server_config.get("headers", {})
    auth_header = headers.get("Authorization", headers.get("authorization", ""))
    if isinstance(auth_header, str) and auth_header.startswith("Bearer "):
        return AuthMode.service_account
    return AuthMode.per_user_oauth


def auto_display_name(mcp_id: str) -> str:
    """Generate a display name from an MCP id."""
    return mcp_id.replace("-", " ").replace("_", " ").title()


_RESOURCE_FIELDS: tuple[str, ...] = (
    "cpu_vcpus", "memory_mb", "disk_gb",
    "pids_limit", "tmpfs_mb", "persistent_disk_enabled",
)


def server_config_from_upstream(upstream: UpstreamDefinition) -> dict[str, Any]:
    """Serialize an upstream's transport config into a mcpServers-style
    dict, including any non-default sandbox resource fields.

    Resource fields are only emitted when they differ from
    ``StdioTransportConfig`` defaults so existing mcp.json fixtures and
    JSON-based imports stay clean. The reverse parse in
    :func:`build_upstream` reads these same keys back, giving end-to-end
    persistence across restarts.
    """
    server_config: dict[str, Any] = {}
    if upstream.stdio:
        stdio = upstream.stdio
        server_config["command"] = stdio.command
        if stdio.args:
            server_config["args"] = stdio.args
        if stdio.env:
            server_config["env"] = stdio.env
        defaults = StdioTransportConfig(command=stdio.command)
        for field in _RESOURCE_FIELDS:
            value = getattr(stdio, field)
            if value != getattr(defaults, field):
                server_config[field] = value
    elif upstream.http:
        server_config["url"] = upstream.http.url
        if upstream.http.headers:
            server_config["headers"] = upstream.http.headers
    return server_config


def build_upstream(
    mcp_id: str,
    server_config: dict[str, Any],
    options: dict[str, Any],
    oauth_apps: OAuthAppsConfig | None = None,
    allow_stdio: bool = True,
) -> UpstreamDefinition:
    """Build an UpstreamDefinition from a mcpServers entry + optional YAML overrides."""
    # Transport
    if "command" in server_config:
        if not allow_stdio:
            raise ValueError("Stdio MCP servers are disabled")
        transport = TransportType.stdio
        stdio_kwargs: dict[str, Any] = {
            "command": server_config["command"],
            "args": server_config.get("args", []),
            "env": server_config.get("env", {}),
        }
        for field in _RESOURCE_FIELDS:
            if field in server_config:
                stdio_kwargs[field] = server_config[field]
        stdio = StdioTransportConfig(**stdio_kwargs)
        http = None
    else:
        transport = TransportType.streamable_http
        stdio = None
        http = HttpTransportConfig(
            url=server_config["url"],
            headers=server_config.get("headers", {}),
        )

    # Auth — YAML override or auto-detect
    auth_mode_str = options.get("auth_mode")
    if auth_mode_str:
        auth_mode = AuthMode(auth_mode_str)
    else:
        auth_mode = auto_detect_auth_mode(server_config)

    # Coerce stale stdio + OAuth combos forward. The model validator
    # below (and every consuming code path) treats stdio + non-
    # service_account as a non-functional shape, so a legacy row from
    # before the validator landed would be silently dropped at load
    # time. Force it back to the only mode that actually works for
    # stdio and log a warning so the operator can see what changed.
    if (
        transport == TransportType.stdio
        and auth_mode != AuthMode.service_account
    ):
        logger.warning(
            "upstream_config.stdio_auth_mode.coerced",
            upstream_id=mcp_id,
            stale_auth_mode=auth_mode.value,
        )
        auth_mode = AuthMode.service_account

    # Extract token from headers if service_account
    token = None
    if auth_mode == AuthMode.service_account and http:
        auth_header = http.headers.get("Authorization", http.headers.get("authorization", ""))
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    auth = UpstreamAuthConfig(
        mode=auth_mode,
        token=token,
        client_id=options.get("client_id"),
        client_secret=options.get("client_secret"),
        scopes=options.get("scopes", []),
    )

    # Domain-matched fallback if no per-upstream client_id
    if auth.client_id is None and oauth_apps and http:
        resolved = resolve_oauth_app(http.url, oauth_apps)
        if resolved is not None:
            app_entry, matched_domain = resolved
            auth = auth.model_copy(update={
                "client_id": app_entry.client_id,
                "client_secret": app_entry.client_secret,
                "matched_domain": matched_domain,
            })

    return UpstreamDefinition(
        id=mcp_id,
        display_name=options.get("display_name", auto_display_name(mcp_id)),
        transport=transport,
        stdio=stdio,
        http=http,
        auth=auth,
        default_arguments=options.get("default_arguments", {}),
    )


def load_merged_config(
    mcp_json_path: Path,
    upstream_options: dict[str, dict[str, Any]] | None = None,
    oauth_apps: OAuthAppsConfig | None = None,
    allow_stdio: bool = True,
) -> list[UpstreamDefinition]:
    """Load upstream definitions by merging mcp.json + upstream options.

    mcp.json contains standard mcpServers connection definitions.
    upstream_options (from config.json) contains MCPolis-specific overrides.
    MCPs in mcp.json without options get sensible defaults.
    """
    # Read mcp.json
    servers: dict[str, dict[str, Any]] = {}
    if mcp_json_path.exists():
        import json
        try:
            data = json.loads(mcp_json_path.read_text())
            servers = data.get("mcpServers", {})
        except (json.JSONDecodeError, OSError):
            pass

    if not servers:
        return []

    options_map = upstream_options or {}

    # Merge and build
    result: list[UpstreamDefinition] = []
    for mcp_id, server_config in servers.items():
        options = options_map.get(mcp_id, {})
        try:
            result.append(build_upstream(
                mcp_id, server_config, options,
                oauth_apps=oauth_apps, allow_stdio=allow_stdio,
            ))
        except ValueError as e:
            logger.warning(
                "upstream_config.upstream.skipped",
                upstream_id=mcp_id,
                reason=str(e),
            )

    return result


# --- Bulk import: flatten a pasted/dropped MCP config blob ----------------

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _slugify(raw: str) -> str:
    """Lowercase + collapse disallowed runs into a single dash; trim dashes.

    Mirrors the upstream-id charset the dashboard's ``IdInput`` enforces
    (``[a-z0-9._-]``) and the strategy in
    :func:`mcpolis.domain.model.sandbox_file.slugify_sandbox_file_name`.
    Returns ``""`` when nothing usable remains.
    """
    return _SLUG_RE.sub("-", raw.strip().lower()).strip("-")


def _project_basename(project_path: str) -> str:
    """Last path component of a project path, OS-agnostic (``/`` and ``\\``).

    ``.claude.json`` keys projects by the absolute path on the machine that
    wrote the file, which may use either separator.
    """
    for part in reversed(re.split(r"[/\\]+", project_path.strip())):
        if part:
            return part
    return ""


class ParsedDuplicateRef(BaseModel):
    """Points at the first occurrence of a byte-identical server config."""

    proposed_id: str
    group_label: str


class ParsedImportEntry(BaseModel):
    """One importable MCP server resolved from an import blob.

    ``scope`` is ``"project"`` (from ``.claude.json`` ``projects.*``),
    ``"user"`` (top-level ``mcpServers`` in a ``.claude.json``), or
    ``"standard"`` (a plain ``mcpServers`` / ``servers`` file). ``proposed_id``
    is collision-free against existing upstreams and earlier entries.
    """

    scope: str
    project_path: str | None = None
    group_label: str
    original_id: str
    config: dict[str, Any]
    proposed_id: str
    duplicate_of: ParsedDuplicateRef | None = None


def extract_import_entries(
    data: dict[str, Any],
    existing_ids: Iterable[str] = (),
) -> list[ParsedImportEntry]:
    """Flatten an imported MCP config blob into grouped import entries.

    Envelopes recognized, in order:

    1. ``.claude.json`` — ``data["projects"]`` is a dict whose values carry
       ``mcpServers``. User-scope entries (top-level ``mcpServers``) come
       first, then each project's entries in file order.
    2. ``{"mcpServers": {...}}`` (Claude Desktop / standard).
    3. ``{"servers": {...}}`` (VS Code).

    Anything else returns ``[]`` (the caller raises the import 400s).

    Each entry gets a collision-free ``proposed_id``: project-scoped ids are
    suffixed with the slugified project basename; user/standard ids stay bare.
    A numeric ``-2`` / ``-3`` suffix is appended only when needed to stay
    unique against ``existing_ids`` and earlier entries. Byte-identical configs
    across scopes are kept (not deduped) but flagged via ``duplicate_of`` so
    the UI can surface them.
    """
    collected: list[ParsedImportEntry] = []

    def add_source(
        scope: str,
        project_path: str | None,
        group_label: str,
        mcp_servers: Any,
    ) -> None:
        if not isinstance(mcp_servers, dict):
            return
        for original_id, config in cast("dict[str, Any]", mcp_servers).items():
            if not isinstance(config, dict):
                continue
            config_dict = cast("dict[str, Any]", config)
            if "url" not in config_dict and "command" not in config_dict:
                continue
            collected.append(ParsedImportEntry(
                scope=scope,
                project_path=project_path,
                group_label=group_label,
                original_id=original_id,
                config=config_dict,
                proposed_id="",  # assigned below
            ))

    projects_raw = data.get("projects")
    projects: dict[str, Any] | None = (
        cast("dict[str, Any]", projects_raw)
        if isinstance(projects_raw, dict) else None
    )
    is_claude_json = projects is not None and any(
        isinstance(p, dict)
        and isinstance(cast("dict[str, Any]", p).get("mcpServers"), dict)
        for p in projects.values()
    )
    if is_claude_json and projects is not None:
        add_source("user", None, "User scope", data.get("mcpServers"))
        for path, proj in projects.items():
            if isinstance(proj, dict):
                add_source(
                    "project", path,
                    _project_basename(path) or path,
                    cast("dict[str, Any]", proj).get("mcpServers"),
                )
    elif isinstance(data.get("mcpServers"), dict):
        add_source("standard", None, "Servers", data["mcpServers"])
    elif isinstance(data.get("servers"), dict):
        add_source("standard", None, "Servers", data["servers"])

    if not collected:
        return []

    # Assign collision-free proposed ids (seeded with what already exists).
    used: set[str] = set(existing_ids)
    for entry in collected:
        base = _slugify(entry.original_id)
        if entry.scope == "project":
            # Scope-first prefix, matching the product's universal
            # convention (tool names ``{upstream}__{tool}``, store keys
            # ``enabled:{id}``, event names ``upstream.*``). Separator stays
            # ``-`` — never ``__``, which the gateway reserves to split
            # ``{upstream_id}__{tool}`` and would corrupt tool routing.
            proj_slug = _slugify(entry.group_label)
            if proj_slug:
                base = f"{proj_slug}-{base}" if base else proj_slug
        if not base:
            base = "mcp"
        candidate = base
        n = 2
        while candidate in used:
            candidate = f"{base}-{n}"
            n += 1
        entry.proposed_id = candidate
        used.add(candidate)

    # Flag byte-identical configs (first occurrence wins).
    seen: list[ParsedImportEntry] = []
    for entry in collected:
        for prior in seen:
            if prior.config == entry.config:
                entry.duplicate_of = ParsedDuplicateRef(
                    proposed_id=prior.proposed_id,
                    group_label=prior.group_label,
                )
                break
        seen.append(entry)

    return collected
