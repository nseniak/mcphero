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


# The env var a stdio MCP reads its service-account token from. It is
# also the name of the secret Variable an ``auth_token`` is saved as.
STDIO_AUTH_TOKEN_ENV = "MCP_AUTH_TOKEN"
AUTH_TOKEN_VARIABLE = STDIO_AUTH_TOKEN_ENV
AUTH_TOKEN_REFERENCE = f"${{{AUTH_TOKEN_VARIABLE}}}"


def with_service_account_token(
    upstream: UpstreamDefinition, token: str | None,
) -> tuple[UpstreamDefinition, str | None]:
    """Point *upstream* at its service-account token, and return the
    token to save as the secret Variable ``MCP_AUTH_TOKEN``.

    The transport config gets a reference, never the token: the
    ``Authorization: Bearer ${MCP_AUTH_TOKEN}`` header for HTTP, the
    ``MCP_AUTH_TOKEN: ${MCP_AUTH_TOKEN}`` env var for stdio. Both stores
    persist the transport config, so the reference survives a reload,
    and the Variable keeps the token masked in the dashboard, hidden
    from ``get_upstream`` and redacted from Server logs (what "Move to
    Variables" does). A separate token field was dropped on save, and
    its copy of a ``Bearer ${NAME}`` header overwrote the substituted one.

    Returns ``(upstream, None)`` unchanged when there is nothing to save:
    no token, an OAuth mode (no static token), or a header or env entry
    the caller already set (after "Move to Variables" the dashboard sends
    ``Bearer ${NAME}`` alongside the raw token).

    Surrounding whitespace is trimmed (a token read from a file ends in
    a newline). See ``_check_service_account_token`` for what is refused.
    """
    token = (token or "").strip()
    if not token or upstream.auth.mode != AuthMode.service_account:
        return upstream, None
    if upstream.http is not None:
        headers = upstream.http.headers
        if any(name.lower() == "authorization" for name in headers):
            return upstream, None
        _check_service_account_token(token, ascii_only=True)
        http = upstream.http.model_copy(update={
            "headers": {
                **headers, "Authorization": f"Bearer {AUTH_TOKEN_REFERENCE}",
            },
        })
        return upstream.model_copy(update={"http": http}), token
    if upstream.stdio is not None:
        env = upstream.stdio.env
        if STDIO_AUTH_TOKEN_ENV in env:
            return upstream, None
        _check_service_account_token(token, ascii_only=False)
        stdio = upstream.stdio.model_copy(update={
            "env": {**env, STDIO_AUTH_TOKEN_ENV: AUTH_TOKEN_REFERENCE},
        })
        return upstream.model_copy(update={"stdio": stdio}), token
    return upstream, None


def has_service_account_token(upstream: UpstreamDefinition) -> bool:
    """Whether a service-account upstream sends a token: an
    ``Authorization`` header (HTTP) or an ``MCP_AUTH_TOKEN`` env var
    (stdio), the places ``with_service_account_token`` writes."""
    if upstream.auth.mode != AuthMode.service_account:
        return False
    if upstream.http is not None:
        return any(
            name.lower() == "authorization" for name in upstream.http.headers
        )
    if upstream.stdio is not None:
        return STDIO_AUTH_TOKEN_ENV in upstream.stdio.env
    return False


def _check_service_account_token(token: str, *, ascii_only: bool) -> None:
    """Raise ``ValueError`` when *token* can never be sent.

    A control character (a line break inside the token) and, in an HTTP
    header, a non-ASCII character make the HTTP client refuse every
    request, and its error message prints the whole header, token
    included, into the logs. The message raised here never contains the
    token. Callers translate it to a 400 / ``Error:`` reply.
    """
    if not token.isprintable():
        raise ValueError(
            "auth_token contains a line break, a tab or another "
            "non-printable character"
        )
    if ascii_only and not token.isascii():
        raise ValueError(
            "auth_token for an HTTP MCP must use ASCII characters only"
        )


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
