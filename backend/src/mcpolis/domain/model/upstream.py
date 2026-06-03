from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig


class TransportType(StrEnum):
    stdio = "stdio"
    streamable_http = "streamable_http"


def validate_stdio_uses_service_account(
    transport: TransportType,
    auth_mode: AuthMode,
) -> None:
    """Stdio MCPs can only use ``service_account`` auth.

    OAuth modes (``admin_oauth`` / ``per_user_oauth``) require an HTTP
    upstream the gateway can drive the OAuth handshake against; stdio
    wrappers can't terminate OAuth in their sandbox (see
    ``docs/stdio-authent.md`` §D). Every code path that consumes
    ``auth.mode`` for OAuth-shaped behaviour first checks
    ``upstream.http is not None``, so the OAuth modes on stdio are
    silently dead — the upstream becomes unconnectable. Reject the
    combo at the model boundary so neither the dashboard nor the
    admin MCP can persist a non-functional shape.

    Raises ``ValueError``; callers translate to HTTP 400.
    """
    if (
        transport == TransportType.stdio
        and auth_mode != AuthMode.service_account
    ):
        raise ValueError(
            f"Stdio MCPs only support auth_mode='service_account' "
            f"(got {auth_mode.value!r}). Stdio wrappers can't terminate "
            f"OAuth in the sandbox; see docs/stdio-authent.md."
        )


class StdioTransportConfig(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    # Per-MCP resource configuration. Validated against the active
    # provider's ``SandboxCapabilities`` at save time and again at
    # session start; off-grid values raise ``ResourcesUnsupported``
    # before any sandbox is spawned. Defaults are picked to validate
    # on every backend's grid (cf. plan §"Per-MCP resource
    # configuration"). The conversion from these flat fields to a
    # ``SandboxResources`` happens in
    # ``UpstreamClientManager._create_task``.
    cpu_vcpus: float = 1.0
    memory_mb: int = 1024
    # 0 ⇔ provider default / ephemeral. The own runner maps non-zero
    # values to a Phase D loopback ext4 image; on E2B the field is
    # informational because storage is fixed at template build time.
    disk_gb: int = 0
    # Optional per-MCP cap on the number of processes/threads the
    # sandbox is allowed to spawn. ``None`` ⇔ runner default.
    pids_limit: int | None = None
    # Optional per-MCP /tmp tmpfs sizing in MiB. ``None`` ⇔ runner
    # default; non-None overrides ``limits.Default().TmpfsBytes``.
    tmpfs_mb: int | None = None

    # Opt-in per-MCP persistent storage. On E2B this maps to a
    # ``/data`` Volume (created lazily at first session, destroyed
    # on upstream removal). Off by default — most MCPs work fine
    # with ephemeral storage. Opt in when the MCP needs a warm
    # ``npx`` / ``uvx`` package cache or local state across sessions.
    persistent_disk_enabled: bool = False


class HttpTransportConfig(BaseModel):
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class UpstreamDefinition(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    display_name: str
    transport: TransportType

    stdio: StdioTransportConfig | None = None
    http: HttpTransportConfig | None = None

    auth: UpstreamAuthConfig

    # Static arguments injected into tool calls, keyed by original tool name
    default_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)

    PREFIX_SEPARATOR: str = "__"

    @model_validator(mode="after")
    def _check_transport_auth_combo(self) -> UpstreamDefinition:
        validate_stdio_uses_service_account(self.transport, self.auth.mode)
        return self


class ServerInfo(BaseModel):
    name: str
    version: str
    title: str | None = None


class UpstreamSelfDescription(BaseModel):
    """Free-form text the upstream advertises about itself at ``initialize``.

    Built from the upstream's ``InitializeResult``: ``serverInfo.name`` /
    ``version``, the optional top-level ``instructions`` string, and any
    ``description`` / ``websiteUrl`` fields the server attaches to
    ``serverInfo`` (Notion fills in ``description`` empirically;
    ``Implementation`` allows extras). Captured at connect time so the
    gateway can fold it into its own ``initialize`` instructions and
    let the LLM see *what* each upstream actually does.

    ``capabilities_extensions`` mirrors ``InitializeResult.capabilities.
    extensions`` — a free-form ``dict[str, dict[str, Any]]`` keyed by
    extension URI (e.g. ``"io.modelcontextprotocol/ui"``). Captured so
    the gateway can re-advertise the union across connected upstreams
    in its own ``initialize`` response (without it, MCP-Apps clients
    refuse to fetch ``ui://`` widget resources).
    """

    name: str
    version: str
    instructions: str | None = None
    description: str | None = None
    website_url: str | None = None
    capabilities_extensions: dict[str, dict[str, Any]] = Field(
        default_factory=dict[str, dict[str, Any]],
    )


class ToolAnnotations(BaseModel):
    title: str | None = None
    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None
    idempotentHint: bool | None = None
    openWorldHint: bool | None = None

    def to_flags(self) -> dict[str, bool]:
        """Return annotation hints as {name: value} for policy matching."""
        flags: dict[str, bool] = {}
        if self.readOnlyHint is not None:
            flags["readOnly"] = self.readOnlyHint
        if self.destructiveHint is not None:
            flags["destructive"] = self.destructiveHint
        if self.idempotentHint is not None:
            flags["idempotent"] = self.idempotentHint
        if self.openWorldHint is not None:
            flags["openWorld"] = self.openWorldHint
        return flags


class DiscoveredTool(BaseModel):
    upstream_id: str
    original_name: str
    prefixed_name: str
    description: str | None
    input_schema: dict[str, Any]
    title: str | None = None
    output_schema: dict[str, Any] | None = None
    annotations: ToolAnnotations | None = None
    # Verbatim ``_meta`` from the upstream tool definition, kept under
    # the plain ``meta`` name and re-emitted at the wire boundary via
    # ``mcp_types.Tool.model_validate({..., "_meta": meta})``. The MCP
    # SDK's ``Tool`` aliases this field (``Field(alias="_meta")`` with
    # no ``populate_by_name``), so constructing ``Tool(meta=...)``
    # silently drops it — see FINDINGS §3.
    meta: dict[str, Any] | None = None


class DiscoveredResource(BaseModel):
    """A resource discovered on an upstream MCP server.

    ``original_uri`` is the URI the upstream uses. The wrapped URI
    surfaced to downstream clients is built per-request by
    ``uri_wrapping.wrap_resource_uri`` because it needs the per-request
    org slug (which the registry does not know).
    """

    upstream_id: str
    original_uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    meta: dict[str, Any] | None = None


class DiscoveredResourceTemplate(BaseModel):
    """A resource template discovered on an upstream MCP server.

    Templates are advertised via ``resources/templates/list`` and
    described by a URI template (RFC 6570) rather than a concrete URI.
    Forwarded for completeness — clients that build URIs from templates
    still need to round-trip them through a wrapped ``resources/read``.
    """

    upstream_id: str
    original_uri_template: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    meta: dict[str, Any] | None = None


class PromptArgument(BaseModel):
    name: str
    description: str | None = None
    required: bool | None = None


class DiscoveredPrompt(BaseModel):
    """A prompt template discovered on an upstream MCP server."""

    upstream_id: str
    original_name: str
    prefixed_name: str
    title: str | None = None
    description: str | None = None
    arguments: list[PromptArgument] = Field(default_factory=list[PromptArgument])
    meta: dict[str, Any] | None = None
