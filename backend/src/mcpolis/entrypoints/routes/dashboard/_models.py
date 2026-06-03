"""Pydantic models shared across dashboard route files.

Lives in the ``dashboard`` package so per-concern files can import the
same wire-shape definitions without circular-importing each other (or
the top-level ``dashboard_api`` module). Models are grouped by concern
in source order; the file is alphabetized inside each group.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    ToolAccessConfig,
)
from mcpolis.domain.model.upstream import ServerInfo


# --- Upstream summary / detail (admin tab listing + detail panel) ---


class UpstreamSummary(BaseModel):
    id: str
    display_name: str
    transport: str
    auth_mode: str
    # Org-level "this upstream is usable right now" gate. For
    # ``service_account`` that means the shared session is live; for
    # both OAuth modes it means at least one admin has authenticated
    # (has a stored token row). The OAuth modes are deliberately
    # uniform here — Ready ⇔ admin authenticated.
    ready: bool
    # Email of the admin currently providing readiness for either
    # OAuth mode, or ``None`` for service_account / when no admin is
    # signed in. Drives the admin tab's "Authenticated by alice@" +
    # take-over UX. When multiple admins have rows for the same
    # ``per_user_oauth`` upstream, the most-recently updated row wins
    # so the displayed owner is stable across requests.
    slot_owner: str | None
    tool_count: int
    # True while the post-connect catalog refresh (list_tools /
    # list_resources / list_prompts) is running in the background.
    # Drives the dashboard's "Fetching info" indicator so users know a
    # 0 tool_count is transient on slow upstreams (e.g. Mixpanel cold).
    refreshing: bool = False
    # True while a fire-and-forget reconnect task is in flight on
    # the backend (admin clicked Start/Reconnect, the background
    # ``connect_shared`` hasn't finished yet — succeed or fail).
    # Drives the dashboard's disabled "Starting…" pill across tabs
    # and refreshes. Replaces the old ``sandbox_state`` signal that
    # came from the deleted lifecycle pill / state registry; pure
    # in-process truth, free to compute (no E2B round-trip).
    starting: bool = False
    url: str | None = None
    disconnect_reason: str | None = None


class ConnectedUser(BaseModel):
    """Per-user connection metadata exposed on upstream detail.

    Used by the admin tab's three-state Connections section so the
    viewer can tell who is connected, when their token expires, and
    whether they are an admin (relevant for ``admin_oauth`` displays).
    """

    email: str
    expires_at: datetime | None = None
    is_admin: bool = False


class SandboxResourcesView(BaseModel):
    """Wire-format mirror of ``StdioTransportConfig`` resource fields.

    The admin UI reads this for the current per-MCP CPU/RAM/disk
    settings + cross-references against
    ``GET /api/admin/sandbox/capabilities`` to render the active
    provider's allowed grid as form controls.
    """
    cpu_vcpus: float
    memory_mb: int
    disk_gb: int
    pids_limit: int | None = None
    tmpfs_mb: int | None = None
    persistent_disk_enabled: bool = False


class UpstreamDetail(BaseModel):
    id: str
    display_name: str
    transport: str
    auth_mode: str
    # See ``UpstreamSummary.ready`` / ``slot_owner``.
    ready: bool
    slot_owner: str | None
    # Same fire-and-forget reconnect signal as ``UpstreamSummary.starting``.
    # Used by the detail page's button + status pill to render
    # "Starting…" across tab switches and reloads while a background
    # ``connect_upstream`` task is in flight.
    starting: bool = False
    url: str | None = None
    command: str | None = None
    client_id: str | None = None
    has_client_secret: bool = False
    oauth_app_domain: str | None = None
    oauth_app_client_id: str | None = None
    scopes: list[str]
    default_arguments: dict[str, Any]
    server_config: dict[str, Any] = {}  # Raw mcpServers JSON entry
    server_info: ServerInfo | None = None
    disconnect_reason: str | None = None
    connected_users: list[ConnectedUser] = []
    # Per-MCP resource configuration (step 5 + 11 of the SandboxService
    # rollout). Populated for stdio upstreams; ``None`` for HTTP. The
    # admin UI's resource picker reads ``sandbox_resources`` for the
    # current values + ``GET /api/admin/sandbox/capabilities`` for the
    # active provider's allowed grid.
    sandbox_resources: SandboxResourcesView | None = None
    # ``True`` when the upstream is running but its persisted config
    # (or env-var set) has drifted from the snapshot the running
    # session was started with. Drives the detail page's "stop &
    # restart" dirty banner. Always ``False`` while the upstream is
    # not ready / starting up — those states have no live session to
    # diverge from.
    is_dirty: bool = False
    # SHA-256 fingerprint of the saved config + env-var set as of
    # this read. The frontend keys its per-MCP "Dismiss" sessionStorage
    # entry by this value, so a fresh save automatically re-shows the
    # banner even if the user dismissed the previous version.
    config_hash: str | None = None


# --- Tools ---


class ToolAnnotationsInfo(BaseModel):
    title: str | None = None
    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None
    idempotentHint: bool | None = None
    openWorldHint: bool | None = None


class ToolInfo(BaseModel):
    upstream_id: str
    original_name: str
    prefixed_name: str
    description: str | None
    input_schema: dict[str, Any] = {}
    title: str | None = None
    output_schema: dict[str, Any] | None = None
    annotations: ToolAnnotationsInfo | None = None


# --- Audit ---


class AuditSearchResponse(BaseModel):
    entries: list[dict[str, Any]]
    count: int


# --- Users + roles ---


class UserInfo(BaseModel):
    email: str
    role: str
    is_admin: bool
    status: str = "active"  # "active" (signed in) or "pending" (pre-approved)


class RoleSummary(BaseModel):
    name: str
    is_admin: bool
    is_default: bool
    user_count: int


# --- Per-viewer /my-tools listing ---


class UserToolSummary(BaseModel):
    name: str
    description: str | None


class UserMcpInfo(BaseModel):
    id: str
    display_name: str
    transport: str
    auth_mode: str
    # Org-level Ready — same value the admin tab sees. For OAuth
    # modes this means an admin has authenticated. ``False`` makes
    # /my-tools render the row as Unavailable. The per-viewer
    # signed-in state is reported separately as
    # ``user_connection_status`` (only meaningful for
    # ``per_user_oauth``).
    ready: bool
    url: str | None = None
    user_connection_status: str
    tool_count: int = 0
    tools: list[UserToolSummary] = []


# --- OAuth connect/reconnect responses (auth_connect + upstream_admin) ---


class ConnectResponse(BaseModel):
    authorization_url: str | None = None
    connected: bool = False
    error: str | None = None
    # Fire-and-forget reconnect outcome. ``"pending"`` means the
    # backend kicked off a detached connect task and the caller
    # should stop waiting on this response — the
    # ``sandbox_state_changed`` SSE stream carries warming → active
    # → ready / failed transitions live, and the upstream's
    # ``ready`` / ``disconnect_reason`` flips on a subsequent fetch.
    # Unset on the legacy synchronous OAuth flow to keep that
    # branch's response shape unchanged.
    outcome: str | None = None
    upstream_id: str | None = None


# --- Request bodies ---


class AddUpstreamRequest(BaseModel):
    id: str
    display_name: str
    # HTTP transport
    url: str | None = None
    headers: dict[str, str] = {}
    # Stdio transport
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    # Stdio sandbox resources (None → fall back to model defaults).
    # Validated against the active provider's grid at create time.
    cpu_vcpus: float | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    pids_limit: int | None = None
    tmpfs_mb: int | None = None
    persistent_disk_enabled: bool | None = None
    # Auth
    auth_mode: str = "service_account"
    auth_token: str = ""
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = []
    # Optional per-MCP env vars to create alongside the upstream so
    # the frontend create wizard can submit them in a single round
    # trip. Each entry carries the value plus an ``is_secret`` flag —
    # secret values are masked after save, plain values stay visible
    # in the UI. ``None`` (the default) is treated identically to ``{}``.
    template_vars: dict[str, "AddUpstreamTemplateVarSpec"] | None = None


class AddUpstreamTemplateVarSpec(BaseModel):
    """Per-entry shape inside ``AddUpstreamRequest.env_vars``."""

    value: str
    is_secret: bool = True


class TemplateVarSummaryView(BaseModel):
    """Wire shape for a per-MCP template-variable summary.

    Both kinds carry the plaintext ``value`` — the SPA obfuscates
    password rows by default and exposes an eye toggle to reveal
    (1Password-style). ``last_four`` is still populated for the
    masked preview placeholder.
    """

    name: str
    is_secret: bool
    value: str | None
    last_four: str | None
    created_at: datetime
    updated_at: datetime


class SandboxFileSummaryView(BaseModel):
    """Wire shape for a per-MCP Sandbox file summary.

    Never carries ``contents`` — admins upload files but the UI
    renders only metadata (size + sha256 + last-modified).
    ``name`` is the URL-safe storage key (the ``{name}`` path
    component on PUT / DELETE); ``display_name`` is the free-form
    label rendered in the listing.
    """

    name: str
    display_name: str
    target_path: str
    sha256: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime


class SetSandboxFileRequest(BaseModel):
    """Body of ``PUT /api/admin/upstreams/{id}/sandbox-files/{name}``.

    ``contents`` is plaintext on the wire (TLS protects it in
    transit; encryption-at-rest is the storage boundary). The
    ``target_path`` accepts the same ``${...}`` references env-var
    values accept (system + user Variables). ``display_name`` is
    optional — when omitted (or empty) the backend stores the URL
    ``{name}`` path component as the display label so legacy
    scripted callers keep working.
    """

    contents: str
    target_path: str
    display_name: str | None = None


class SystemVariableView(BaseModel):
    """Wire shape for a system Variable (``${HOME}``, …).

    Read-only — the value is computed from the live sandbox env at
    launch time, not persisted per-upstream. Surfaced in the
    Variables list with a "system" badge so operators can
    discover/reference them.
    """

    name: str
    value: str


class SetTemplateVarRequest(BaseModel):
    """Body of ``PUT /api/admin/upstreams/{id}/template-vars/{name}``.

    ``is_secret`` is a **create-time** decision — the server-side
    repository preserves the existing record's flag on replace, so
    flipping a value's secrecy after the fact requires delete +
    re-create.
    """

    value: str
    is_secret: bool = True


class AddUserRequest(BaseModel):
    email: str
    role: str | None = None


class UpdateUpstreamTemplateVarSpec(BaseModel):
    """Per-entry shape inside ``UpdateUpstreamRequest.template_var_changes.sets``.

    Mirrors :class:`AddUpstreamTemplateVarSpec` so the buffered create-wizard
    payload and the deferred edit-page payload share a wire shape.
    """

    value: str
    is_secret: bool = True


class UpdateUpstreamTemplateVarChanges(BaseModel):
    """Buffered env-var mutations the deferred Edit/Save flow flushes
    in the same request that updates the upstream config.

    Validated up-front: a bad name or empty value rejects the whole
    save with a 400 before any sub-mutation is applied. ``sets`` and
    ``deletes`` may name the same key; ``deletes`` wins (the create
    is dropped before apply, matching the user-visible "I added then
    deleted in one session" intent).
    """

    sets: dict[str, UpdateUpstreamTemplateVarSpec] = {}
    deletes: list[str] = []


class UpdateSandboxResourcesPatch(BaseModel):
    """Patch shape for stdio sandbox resources inside
    :class:`UpdateUpstreamRequest`.

    Only the explicitly-set fields are applied; ``None`` for any
    optional field means "leave the current value alone." The route
    revalidates the resulting combo against the active provider's
    grid and rejects with a 400 + ``{message, field, value}`` so the
    admin form can flag the offending control.
    """
    cpu_vcpus: float | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    pids_limit: int | None = None
    tmpfs_mb: int | None = None
    persistent_disk_enabled: bool | None = None


class UpdateUpstreamRequest(BaseModel):
    display_name: str | None = None
    auth_mode: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    server_config: dict[str, Any] | None = None
    template_var_changes: UpdateUpstreamTemplateVarChanges | None = None
    # Stdio sandbox resources patch (CPU / RAM / disk / pids / tmpfs /
    # persistent volume opt-in). The detail-page edit form now folds
    # the resource picker into the SETTINGS Save flow — same
    # pattern as ``template_var_changes`` — so a single PUT commits
    # display-name + auth + JSON + env vars + resources atomically.
    # ``None`` (the default) leaves resources untouched.
    sandbox_resources: UpdateSandboxResourcesPatch | None = None


class SetMcpAccessRequest(BaseModel):
    enabled: bool


class SetRoleRequest(BaseModel):
    role: str


class SetRoleMcpAccessRequest(BaseModel):
    """Set entire mcp_access for a role."""
    mcp_access: McpAccessConfig


class SetEnabledRequest(BaseModel):
    enabled: bool


class SetAutoEnableNewRequest(BaseModel):
    auto_enable_new: bool


class SetToolFallbackEnabledRequest(BaseModel):
    fallback_enabled: bool | None


class CreateRoleRequest(BaseModel):
    name: str
    copy_from: str | None = None


class RenameRoleRequest(BaseModel):
    new_name: str


class SetArgumentConstraintRequest(BaseModel):
    pattern: str
    mode: str = "allow"


class RoleAccessInfo(BaseModel):
    name: str
    is_admin: bool
    is_default: bool
    mcp_access: McpAccessConfig
    tool_access: dict[str, ToolAccessConfig] = {}
    argument_constraints: dict[str, dict[str, ArgumentConstraint]] = {}


class ImportFileRequest(BaseModel):
    data: dict[str, Any]


class ImportDuplicateRef(BaseModel):
    """Pointer to the first occurrence of a byte-identical server config.

    Lets the import dialog tag a row "duplicate of <proposed_id> from
    <group_label>" so the operator can deselect redundant copies that
    ``.claude.json`` repeats across projects.
    """

    proposed_id: str
    group_label: str


class ImportEntry(BaseModel):
    """One importable MCP server row in the preview dialog.

    ``scope`` is ``"project"`` / ``"user"`` / ``"standard"``;
    ``group_label`` is the per-group header (project basename, "User scope",
    or "Servers"). ``proposed_id`` is a collision-free default the operator
    can edit inline before confirming. ``original_id`` + ``project_path``
    let confirm re-resolve the raw config server-side.
    """

    scope: str
    project_path: str | None = None
    group_label: str
    original_id: str
    proposed_id: str
    display_name: str
    transport: str
    auth_mode: str
    blocked: bool = False
    blocked_reason: str | None = None
    duplicate_of: ImportDuplicateRef | None = None


class ImportPreviewResponse(BaseModel):
    entries: list[ImportEntry]
    # Ids already taken by existing upstreams — the dialog validates inline
    # id edits against these (and against the other selected rows).
    existing_ids: list[str]
    parse_errors: list[str]


class ImportConfirmEntry(BaseModel):
    """A single confirmed import: which source server, under which final id."""

    scope: str
    project_path: str | None = None
    original_id: str
    target_id: str


class ImportConfirmRequest(BaseModel):
    data: dict[str, Any]
    entries: list[ImportConfirmEntry]


class ImportErrorDetail(BaseModel):
    id: str
    error: str


class ImportResultResponse(BaseModel):
    added: list[str]
    skipped: list[str]
    errors: list[ImportErrorDetail]
