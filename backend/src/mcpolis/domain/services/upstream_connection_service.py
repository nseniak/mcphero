"""Service for initiating OAuth connections to upstream MCP servers.

The OAuth flow requires a background task because the MCP SDK's
OAuthClientProvider blocks during authorization (redirect → browser →
callback → token exchange).  However, the MCP SDK's streamablehttp_client
uses anyio cancel scopes that are bound to the task that created them.
If a session is created in a background task and then used from a different
task, it crashes.

To avoid this, we split the flow:
1. Background task: trigger token acquisition only (lightweight httpx
   request through the OAuthClientProvider — no MCP session).
2. Caller's task: create the real MCP session using the stored tokens.
"""
from __future__ import annotations

import asyncio
import time
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import structlog
from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
)
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
from mcpolis.adapters.auth.pending_auth import (
    PendingAuth,
    PendingAuthCoordinator,
    UpstreamUnreachableError,
)
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.safe_http_transport import (
    SafeAsyncHTTPTransport,
)
from mcpolis.domain.model.upstream import DiscoveredTool, UpstreamDefinition
from mcpolis.domain.services.tool_registry import (
    ToolRegistry,
    is_transport_stall,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


async def _inject_accept_json(request: httpx.Request) -> None:
    """Ensure Accept: application/json on all requests including auth-flow.

    GitHub's token endpoint returns form-encoded data by default;
    this header makes it return JSON that the MCP SDK can parse.
    Workaround for: https://github.com/modelcontextprotocol/typescript-sdk/issues/759
    """
    request.headers.setdefault("Accept", "application/json")


def _unwrap_error(e: BaseException) -> str:
    """Extract a readable message from exceptions, unwrapping ExceptionGroups."""
    subs: tuple[BaseException, ...] = getattr(e, "exceptions", ())
    if subs:
        return _unwrap_error(subs[0])
    return str(e)


class DisconnectReason(StrEnum):
    """Reasons why an OAuth upstream is disconnected."""
    no_tokens = "no_tokens"
    token_expired = "token_expired"
    token_refresh_failed = "token_refresh_failed"
    connection_failed = "connection_failed"
    connection_timeout = "connection_timeout"


class OAuthFailureReason(StrEnum):
    """Why an upstream OAuth connection attempt failed.

    Mirrors the ``failure_reason`` enum on the ``upstream_oauth_failed``
    Mixpanel event documented in ``MIXPANEL_EVENTS.md``.
    """
    user_denied = "user_denied"
    token_exchange = "token_exchange"
    discovery = "discovery"
    upstream_unavailable = "upstream_unavailable"
    unknown = "unknown"


class OAuthConnectResult:
    """Result of an OAuth connection attempt."""

    def __init__(
        self,
        connected: bool = False,
        authorization_url: str | None = None,
        error: str | None = None,
        failure_reason: OAuthFailureReason | None = None,
    ) -> None:
        self.connected = connected
        self.authorization_url = authorization_url
        self.error = error
        self.failure_reason = failure_reason


REFRESH_FAILURE_BODY_LIMIT = 512


@dataclass(frozen=True)
class RefreshFailureSignature:
    """Captured shape of a failed upstream ``refresh_token`` grant.

    Populated by ``_InitializingOAuthClientProvider._handle_refresh_response``
    before the SDK's default handler clears the context's tokens. Lets
    downstream code distinguish transient infrastructure failures
    (5xx, network blip) from genuine ``invalid_grant`` rejections when
    deciding whether to delete the stored refresh token or retry on
    the next cycle. Without this, the SDK only emits a
    ``WARNING: Token refresh failed: <status>`` line and the evidence
    is lost the moment the outer ``except`` deletes the row.
    """
    status_code: int
    body_excerpt: str
    error_code: str | None
    timestamp: datetime

    def to_log_fields(self) -> str:
        """Render as ``status=… error_code=… body=…`` for a single-line
        log entry next to ``set_connection_error``. Exceeded the 80-col
        wrap, but the three fields are what operators want to see
        together when triaging."""
        return (
            f"status={self.status_code} "
            f"error_code={self.error_code!r} "
            f"body={self.body_excerpt!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "body_excerpt": self.body_excerpt,
            "error_code": self.error_code,
            "timestamp": self.timestamp.isoformat(),
        }


def _parse_oauth_error_code(body: str) -> str | None:
    """Extract the ``error`` field (e.g. ``"invalid_grant"``) from an
    OAuth 2.0 error response body. RFC 6749 §5.2 mandates JSON with
    an ``error`` string field; non-conforming upstreams (HTML 5xx
    pages, binary blobs) return ``None`` so callers treat them as
    "unknown code — assume transient"."""
    try:
        data: object = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        err = data.get("error")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(err, str):
            return err
    return None


class _InitializingOAuthClientProvider(OAuthClientProvider):
    """Upstream SDK bug workaround + refresh-failure forensics.

    ``OAuthContext._initialize`` loads stored tokens but never calls
    ``update_token_expiry``, so ``is_token_valid()`` reads
    ``token_expiry_time == None`` as "valid forever" and
    ``async_auth_flow`` skips the refresh_token branch — falling through
    to the authorization_code grant on the next 401, which our silent
    paths block via ``_noop_callback`` (RuntimeError in Sentry).

    The fix: after ``_initialize`` loads tokens, populate
    ``token_expiry_time`` from the adapter-provided ``expires_in`` so
    ``is_token_valid()`` returns ``False`` for an already-expired stored
    token and the refresh branch runs.

    Also: when a refresh response comes back non-200, capture a
    bounded excerpt of the body plus status/error_code into
    ``last_refresh_failure`` *before* the SDK's default handler clears
    the context tokens. That's the only moment where status+body are
    still available — after it, the forensic trail dead-ends in a
    ``WARNING: Token refresh failed`` line.
    """

    last_refresh_failure: RefreshFailureSignature | None = None

    async def _initialize(self) -> None:
        await super()._initialize()  # pyright: ignore[reportPrivateUsage]
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)

    async def _handle_refresh_response(
        self, response: httpx.Response,
    ) -> bool:
        if response.status_code != 200:
            try:
                body_bytes = await response.aread()
                excerpt = body_bytes[:REFRESH_FAILURE_BODY_LIMIT].decode(
                    "utf-8", errors="replace",
                )
            except Exception:
                excerpt = ""
            self.last_refresh_failure = RefreshFailureSignature(
                status_code=response.status_code,
                body_excerpt=excerpt,
                error_code=_parse_oauth_error_code(excerpt),
                timestamp=datetime.now(UTC),
            )
        return await super()._handle_refresh_response(response)  # pyright: ignore[reportPrivateUsage]


def _extract_refresh_failure(
    provider: OAuthClientProvider,
) -> RefreshFailureSignature | None:
    """Pull the captured refresh-failure signature off a provider, if any.

    Falls back to ``None`` when the provider isn't our subclass (tests
    that pass a vanilla ``OAuthClientProvider`` or upstream SDK bumps
    that change the class) — callers treat the missing signature as
    "unknown root cause, fall back to legacy behavior"."""
    if isinstance(provider, _InitializingOAuthClientProvider):
        return provider.last_refresh_failure
    return None


# §5.1 delete-vs-retry tuning knobs. Picked conservatively: five
# consecutive transient failures AND at least half an hour of sustained
# trouble before we wipe the user's token. A single bad minute of network
# can easily produce three failures back-to-back across the 10-min
# periodic interval; five forces a long enough sustained-outage signal
# that we've clearly left "transient" territory. The 30-min window keeps
# us from deleting after a brief multi-failure burst that then stopped.
MAX_CONSECUTIVE_TRANSIENT_FAILURES = 5
MIN_TRANSIENT_FAILURE_WINDOW_SECONDS = 30 * 60


def _should_delete_on_refresh_failure(
    signature: RefreshFailureSignature | None,
    failure_count: int,
    first_failure_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Policy for the outer ``except`` in ``reconnect_with_stored_tokens``.

    Returns True when the stored refresh token should be deleted and the
    user prompted to re-authenticate; False when the failure looks
    transient and the token should stay in place for the next retry.

    - RFC 6749 ``invalid_grant`` → delete immediately. The upstream has
      told us the refresh token is dead; keeping it around just means
      every subsequent tick re-discovers the same rejection and churns
      the §5.4 signature row.
    - Any other failure (5xx, network, unknown) → only delete once we've
      accumulated ``MAX_CONSECUTIVE_TRANSIENT_FAILURES`` failures spanning
      at least ``MIN_TRANSIENT_FAILURE_WINDOW_SECONDS``. Below the
      threshold, keep the token and retry.

    The ``now`` parameter is for testability (inject a fixed clock) —
    callers in production should leave it ``None``.
    """
    if signature is not None and signature.error_code == "invalid_grant":
        return True
    if failure_count < MAX_CONSECUTIVE_TRANSIENT_FAILURES:
        return False
    if first_failure_at is None:
        return False
    current = now if now is not None else datetime.now(UTC)
    elapsed = (current - first_failure_at).total_seconds()
    return elapsed >= MIN_TRANSIENT_FAILURE_WINDOW_SECONDS


async def _build_oauth_provider(
    upstream: UpstreamDefinition,
    storage: McpTokenStorage,
    pending_redirect_handler: Callable[[str], Awaitable[None]],
    pending_callback_handler: Callable[[], Awaitable[tuple[str, str | None]]],
    server_url: str,
) -> OAuthClientProvider:
    """Build an OAuthClientProvider for the given upstream."""
    assert upstream.http is not None
    callback_url = (
        f"{server_url.rstrip('/')}/api/oauth/upstream/callback"
    )
    scopes_str = (
        " ".join(upstream.auth.scopes)
        if upstream.auth.scopes else None
    )
    client_metadata = OAuthClientMetadata(
        redirect_uris=[AnyUrl(callback_url)],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scopes_str,
        client_name="MCP Hero Gateway",
    )

    # Pre-seed client info when a client_id is configured,
    # so the SDK skips dynamic client registration.
    if upstream.auth.client_id:
        await storage.set_client_info(OAuthClientInformationFull(
            client_id=upstream.auth.client_id,
            client_secret=upstream.auth.client_secret,
            redirect_uris=[AnyUrl(callback_url)],
            token_endpoint_auth_method="client_secret_post",
        ))
    # NOTE: stale-redirect self-heal lives in
    # ``_drop_client_info_if_redirect_stale`` and is only invoked from
    # the fresh-consent path (``initiate_oauth_connection``). Doing it
    # here used to wipe stored DCR credentials on every silent path
    # (periodic refresh, liveness probe, …). For ``grant_type=refresh_token``
    # the upstream's token endpoint never inspects ``redirect_uri``
    # (RFC 6749 §6), so dropping client_info on a refresh-only path
    # destroys still-valid refresh credentials for no benefit and the
    # NEXT refresh tick rejects with ``invalid_grant: Client ID mismatch``
    # — exactly the 2026-05-07 mcpolis.seniak.com → mcphero.io rebrand
    # incident. Deferring to the consent path means a callback-URL
    # change is invisible to refresh; only an explicit re-Connect
    # rotates the DCR registration.

    provider = _InitializingOAuthClientProvider(
        server_url=upstream.http.url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=pending_redirect_handler,
        callback_handler=pending_callback_handler,
    )

    # Pre-populate ``OAuthContext.oauth_metadata`` from storage so the
    # SDK's refresh branch resolves the upstream's real
    # ``token_endpoint`` instead of falling back to ``<base>/token``.
    # See §3.8 / §5.4: without this, every periodic refresh on
    # Mixpanel-style upstreams 404s after a process restart, because
    # the SDK only re-discovers metadata on the 401-recovery branch
    # of ``async_auth_flow``, never on the periodic-refresh branch.
    persisted_metadata = await storage.get_oauth_metadata()
    if persisted_metadata is not None:
        provider.context.oauth_metadata = persisted_metadata
        # Hit event: a refresh on this provider will post to this
        # endpoint. Pair with ``oauth.token.refresh.rejected`` if a
        # 404 still happens — the URL will tell you whether the cache
        # was stale (rotate-on-401 will fix it) or correct.
        logger.info(
            "upstream.oauth.metadata.hit",
            upstream_id=upstream.id,
            user=storage.user_id,
            org_id=storage.org_id,
            token_endpoint=str(persisted_metadata.token_endpoint),
        )
    else:
        # Miss event: this provider will fall back to ``<base>/token``
        # if a refresh fires before the SDK's 401-recovery rediscovers.
        # On a Mixpanel-style upstream that's the §3.8 signature.
        logger.info(
            "upstream.oauth.metadata.miss",
            upstream_id=upstream.id,
            user=storage.user_id,
            org_id=storage.org_id,
        )

    return provider


async def _persist_discovered_oauth_metadata(
    provider: OAuthClientProvider, storage: McpTokenStorage,
) -> None:
    """Capture metadata the SDK discovered during a live OAuth flow.

    The SDK populates ``context.oauth_metadata`` lazily — during initial
    consent (``_perform_authorization``) and during 401-recovery. After
    those paths run, persist the discovered value so the next process
    boot can pre-populate it via ``_build_oauth_provider`` and skip the
    §3.8 fall-back. No-op if nothing was discovered (e.g. an upstream
    that hasn't gone through consent yet, or a test that passes a
    mocked-out auth object).
    """
    discovered = provider.context.oauth_metadata
    if not isinstance(discovered, OAuthMetadata):
        return
    await storage.set_oauth_metadata(discovered)
    # Persisted event: pairs with ``upstream.oauth.metadata.miss`` —
    # if you see a miss followed by a persisted within the same flow,
    # legacy rows just got upgraded and the next process boot will
    # see a hit. If you see persisted without a preceding miss, the
    # SDK rediscovered metadata mid-flight (e.g. 401-recovery).
    logger.info(
        "upstream.oauth.metadata.persisted",
        upstream_id=storage.upstream_id,
        user=storage.user_id,
        org_id=storage.org_id,
        token_endpoint=str(discovered.token_endpoint),
    )


async def try_connect_with_stored_tokens(
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore,
    client_manager: UpstreamClientManager,
    server_url: str,
) -> OAuthConnectResult | None:
    """Try to connect using stored tokens. Returns None if no usable tokens."""
    if upstream.http is None:
        return None

    storage = McpTokenStorage(
        connection_store, org_id, upstream.id, effective_user,
        refresh_margin_seconds=TOKEN_REFRESH_MARGIN,
    )
    existing_tokens = await storage.get_tokens()
    if existing_tokens is None:
        return None

    raw_token = await connection_store.get_user_token(
        org_id, effective_user, upstream.id
    )
    if raw_token is None:
        return None

    tokens_usable = (
        raw_token.expires_at is None
        or raw_token.expires_at > datetime.now(UTC)
    )
    if not tokens_usable:
        return None

    oauth_auth = await _build_oauth_provider(
        upstream, storage,
        _noop_redirect, _noop_callback,
        server_url,
    )
    try:
        await client_manager.connect_upstream_for_user(
            upstream, effective_user, auth=oauth_auth
        )
        logger.info(
            "upstream.connect.stored_tokens.success",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
        )
        return OAuthConnectResult(connected=True)
    except Exception:
        logger.warning(
            "upstream.connect.stored_tokens.failed",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            exc_info=True,
        )
        return None


async def _finalize_silent_refresh(
    client_manager: UpstreamClientManager,
    auth_coordinator: PendingAuthCoordinator,
    oauth_auth: OAuthClientProvider,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
) -> OAuthConnectResult:
    """Connect the MCP session after the background task refreshed
    tokens silently (no browser redirect).

    The background task signals "tokens refreshed without redirect"
    by completing ``wait_for_redirect_or_refresh`` with ``None``. At
    that point the stored tokens are fresh; we just need to bring up
    the real MCP session against them and report ``connected=True``
    to the connect-endpoint caller.

    On failure here the auth itself succeeded but the post-auth
    connect failed (e.g. the session-init handshake error'd). The
    user-facing message must distinguish that from the more common
    "auth itself failed" path so the operator triage isn't
    misdirected — hence the explicit
    ``"Authentication succeeded but the connection ... failed"``
    string. Always cleans up the ``PendingAuth`` slot before
    returning so the next attempt starts from a fresh coordinator
    state.
    """
    try:
        await client_manager.connect_upstream_for_user(
            upstream, effective_user, auth=oauth_auth,
        )
        auth_coordinator.cleanup(org_id, upstream.id, effective_user)
        return OAuthConnectResult(connected=True)
    except Exception as e:
        logger.warning(
            "upstream.connect.post_refresh.failed",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            exc_info=True,
        )
        auth_coordinator.cleanup(org_id, upstream.id, effective_user)
        detail = _unwrap_error(e)
        return OAuthConnectResult(
            error=(
                "Authentication succeeded but the connection to this "
                f"MCP failed: {detail}"
            ),
            failure_reason=OAuthFailureReason.unknown,
        )


async def _resume_pending_from_stored_code(
    connection_store: ConnectionStore,
    pending: PendingAuth,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
) -> None:
    """Resume an in-flight OAuth flow from a callback code on disk.

    The OAuth callback endpoint persists the ``(code, state)`` pair
    keyed by ``(org_id, upstream_id, user_id)`` so the redirect can
    arrive even if the server restarts mid-flow. When the user clicks
    "Connect" again after the restart, this helper finds the stored
    code, pre-fills the ``PendingAuth`` so the SDK's
    ``callback_handler`` returns immediately, and pops the code from
    durable storage so it isn't replayed on a third attempt.

    No-op when no code is on disk (the typical first-time-consent
    path); the SDK then waits on the browser redirect normally.
    """
    stored_code = await connection_store.pop_pending_code(
        org_id, upstream.id, effective_user,
    )
    if stored_code is None:
        return
    code, original_state = stored_code
    pending.complete(code, original_state)
    logger.info(
        "upstream.oauth.resume_with_stored_code",
        upstream_id=upstream.id,
        user=effective_user,
        org_id=org_id,
    )


async def _drop_client_info_if_redirect_stale(
    storage: McpTokenStorage,
    upstream: UpstreamDefinition,
    server_url: str,
) -> None:
    """Self-heal stale DCR records on the consent path only.

    If the gateway's callback path changed since the last registration
    (e.g. the 2026-05-07 mcpolis.seniak.com → mcphero.io rebrand, or
    commit 4b43375 moving ``/oauth/upstream/callback`` under ``/api``),
    the upstream's authorize endpoint will reject redirect URIs that
    don't match what it has on file. Drop the stored client so the
    SDK performs a fresh DCR with the current callback URL.

    Only safe to call from the fresh-consent path. Calling from a
    silent path (refresh / liveness probe) destroys still-valid
    refresh credentials: ``grant_type=refresh_token`` doesn't carry
    ``redirect_uri`` (RFC 6749 §6), so a callback-URL change is
    invisible to the upstream's token endpoint — but the next refresh
    after a drop runs against a freshly-DCR'd ``client_id`` that
    doesn't match the one the stored ``refresh_token`` was issued
    under, and the upstream rejects with ``invalid_grant: Client ID
    mismatch``.
    """
    if upstream.auth.client_id:
        # Pre-configured client (not DCR) — nothing to self-heal.
        return
    callback_url = (
        f"{server_url.rstrip('/')}/api/oauth/upstream/callback"
    )
    existing = await storage.get_client_info()
    if existing is None:
        return
    registered_uris = [str(u) for u in (existing.redirect_uris or [])]
    if callback_url in registered_uris:
        return
    logger.warning(
        "upstream.oauth.client_info.stale_redirect_dropped",
        upstream_id=upstream.id,
        registered_redirect_uris=registered_uris,
        current_callback_url=callback_url,
    )
    await storage.delete_client_info()


async def initiate_oauth_connection(
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore,
    auth_coordinator: PendingAuthCoordinator,
    client_manager: UpstreamClientManager,
    server_url: str,
    on_tokens_acquired: Callable[[], None] | None = None,
    on_error: Callable[[str, OAuthFailureReason], None] | None = None,
) -> OAuthConnectResult:
    """Initiate an OAuth connection to an upstream MCP server.

    If stored tokens exist, connects directly (in the current task).
    If no tokens or connection fails, starts a background task that
    only acquires tokens (no MCP session), then returns the
    authorization URL.  The actual MCP session is created later by
    the caller's task after the user completes the OAuth flow.
    """
    if upstream.http is None:
        return OAuthConnectResult(
            error="This MCP does not support authentication (not an HTTP server)."
        )

    # Try connecting with existing tokens first (no PendingAuth needed).
    result = await try_connect_with_stored_tokens(
        org_id, upstream, effective_user, connection_store,
        client_manager, server_url,
    )
    if result is not None:
        return result

    storage = McpTokenStorage(
        connection_store, org_id, upstream.id, effective_user,
        refresh_margin_seconds=TOKEN_REFRESH_MARGIN,
    )

    # Consent path: self-heal stale DCR client_info before the SDK's
    # OAuth flow starts. Silent paths (refresh / liveness) deliberately
    # skip this; see ``_drop_client_info_if_redirect_stale`` docstring.
    await _drop_client_info_if_redirect_stale(storage, upstream, server_url)

    pending = auth_coordinator.create_pending(
        org_id, upstream.id, effective_user
    )

    await _resume_pending_from_stored_code(
        connection_store, pending, org_id, upstream, effective_user,
    )

    oauth_auth = await _build_oauth_provider(
        upstream, storage,
        pending.redirect_handler,
        pending.callback_handler,
        server_url,
    )

    # Start background token acquisition only — no MCP session.
    # The OAuthClientProvider triggers its OAuth flow on the first
    # HTTP request.  We make a lightweight request to the upstream
    # URL; the provider intercepts the 401, runs the authorization
    # flow (redirect_handler → callback_handler → token exchange),
    # and stores the tokens.  The request itself will likely fail
    # (the upstream is an MCP server, not a REST API), but we only
    # care about the side-effect: tokens stored in McpTokenStorage.
    _start_background_token_acquisition(
        upstream, oauth_auth, storage, pending,
        on_tokens_acquired=on_tokens_acquired,
        on_error=on_error,
    )

    # Wait for either a redirect URL (fresh OAuth), a silent token
    # refresh, or a hard failure signal from the background task.
    try:
        auth_url = await pending.wait_for_redirect_or_refresh()
    except TimeoutError:
        auth_coordinator.cleanup(org_id, upstream.id, effective_user)
        return OAuthConnectResult(
            error="Could not reach this MCP server. "
            "Please check the URL and try again.",
            failure_reason=OAuthFailureReason.discovery,
        )
    except UpstreamUnreachableError as e:
        # Background task already classified the error and called
        # on_error — surface the same message synchronously so the
        # connect endpoint returns within seconds instead of waiting
        # the 30s discovery deadline.
        auth_coordinator.cleanup(org_id, upstream.id, effective_user)
        return OAuthConnectResult(
            error=e.user_message,
            failure_reason=OAuthFailureReason.upstream_unavailable,
        )

    if auth_url is None:
        return await _finalize_silent_refresh(
            client_manager, auth_coordinator, oauth_auth,
            org_id, upstream, effective_user,
        )

    # State is now embedded as a signed token in the redirect URL —
    # no need to register it in memory.
    return OAuthConnectResult(authorization_url=auth_url)


def _start_background_token_acquisition(
    upstream: UpstreamDefinition,
    auth: OAuthClientProvider,
    storage: McpTokenStorage,
    pending: PendingAuth,
    on_tokens_acquired: Callable[[], None] | None = None,
    on_error: Callable[[str, OAuthFailureReason], None] | None = None,
) -> asyncio.Task[None]:
    """Start token acquisition in a background task.

    Makes a lightweight HTTP GET to the upstream URL with the
    OAuthClientProvider as auth.  The provider intercepts the
    response (typically 401) and runs the full OAuth flow.
    Once the user completes the browser redirect, the provider
    stores the tokens and we're done.

    If the provider refreshes tokens silently (no browser redirect),
    signals ``pending.mark_tokens_refreshed()`` so the caller
    doesn't wait for a redirect URL that will never come.

    We intentionally do NOT create an MCP session here — that
    avoids the anyio cancel scope cross-task crash.
    """
    assert upstream.http is not None
    url = upstream.http.url

    async def _acquire_tokens() -> None:
        try:
            async with httpx.AsyncClient(
                auth=auth, event_hooks={"request": [_inject_accept_json]},
                transport=SafeAsyncHTTPTransport(),
            ) as client:
                # The request target doesn't matter — the
                # OAuthClientProvider handles the 401 and runs
                # the full OAuth flow as a side-effect.
                await client.get(url)
        except BaseException as e:
            # Expected: the upstream is an MCP server, so the
            # HTTP response may not be clean.  We only care
            # about the tokens being stored.
            logger.debug(
                "upstream.oauth.background_token_acquire.done",
                upstream_id=upstream.id,
                exception_type=type(e).__name__,
                exception_message=str(e),
            )
            # Re-raise CancelledError to respect task cancellation
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                # Still notify error before propagating
                if on_error is not None:
                    on_error(
                        "Authentication was cancelled.",
                        OAuthFailureReason.user_denied,
                    )
                raise

            # Hard upstream failure (5xx / connect error) — short-circuit
            # the 30s wait_for_redirect_or_refresh deadline so the user
            # gets a meaningful error within seconds instead of half a
            # minute of "Could not reach this MCP server."
            unreachable = _classify_upstream_unreachable(e)
            if unreachable is not None:
                user_msg, reason = unreachable
                pending.mark_failed(user_msg)
                if on_error is not None:
                    on_error(user_msg, reason)
                return

        # If the redirect_handler was never called, tokens were
        # refreshed silently — signal this to the caller.
        if not pending.redirect_url:
            tokens = await storage.get_tokens()
            if tokens is not None:
                pending.mark_tokens_refreshed()

        # Notify that tokens have been acquired (for SSE push),
        # or that the flow failed.
        tokens = await storage.get_tokens()
        if tokens is not None:
            # Capture the metadata the SDK discovered during this flow
            # (initial consent or 401 recovery) so the next process
            # boot's ``_build_oauth_provider`` can pre-populate
            # ``OAuthContext.oauth_metadata`` and the periodic refresh
            # branch hits the upstream's real ``token_endpoint``
            # instead of ``<base>/token``. See §3.8 / §5.4.
            await _persist_discovered_oauth_metadata(auth, storage)
            if on_tokens_acquired is not None:
                on_tokens_acquired()
        else:
            if on_error is not None:
                on_error(
                    "Authentication failed — please try again.",
                    OAuthFailureReason.token_exchange,
                )

    return asyncio.create_task(_acquire_tokens())


def _classify_upstream_unreachable(
    exc: BaseException,
) -> tuple[str, OAuthFailureReason] | None:
    """Classify an exception from the background token-acquisition
    request as a hard upstream-side failure (5xx, connect/DNS error).

    Returns ``(user_message, reason)`` if it's a known upstream
    failure we should surface to the user, or ``None`` for everything
    else (e.g. ordinary 401 that the SDK is in the middle of handling).
    """
    status = _extract_http_status(exc)
    if status is not None and 500 <= status < 600:
        return (
            "The MCP server is temporarily unavailable. "
            f"Try again in a minute (HTTP {status} from upstream).",
            OAuthFailureReason.upstream_unavailable,
        )
    if _exception_chain_contains(exc, httpx.ConnectError) or \
            _exception_chain_contains(exc, httpx.ConnectTimeout):
        return (
            "Could not reach this MCP server. "
            "It may be offline — try again in a minute.",
            OAuthFailureReason.upstream_unavailable,
        )
    return None


def _extract_http_status(exc: BaseException) -> int | None:
    """Walk the exception chain looking for an ``httpx.HTTPStatusError``
    and return its response status code, or ``None`` if not found."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    subs: tuple[BaseException, ...] = getattr(exc, "exceptions", ())
    for sub in subs:
        status = _extract_http_status(sub)
        if status is not None:
            return status
    if exc.__cause__ is not None:
        status = _extract_http_status(exc.__cause__)
        if status is not None:
            return status
    if exc.__context__ is not None:
        status = _extract_http_status(exc.__context__)
        if status is not None:
            return status
    return None


async def _noop_redirect(url: str) -> None:
    pass


class SilentReconnectAuthRequired(RuntimeError):
    """Raised by ``_noop_callback`` when the MCP SDK's 401 handler
    falls into the ``authorization_code`` grant during a silent path.

    Definitive proof that the stored credentials cannot recover via
    refresh — the SDK only attempts ``authorization_code`` after a 401
    on a request the SDK considered already-authenticated, AND it
    explicitly does NOT try ``refresh_token`` first
    (``mcp/client/auth/oauth2.py:597``). So if we end up in
    ``authorization_code``, the upstream rejected the bearer for a
    reason refresh wouldn't fix.

    Subclass of ``RuntimeError`` so existing catch-all handlers keep
    working; the type marker lets ``reconnect_with_stored_tokens``
    synthesize an ``invalid_grant`` signature (since no real refresh
    response was ever received to forensic-capture).
    """


async def _noop_callback() -> tuple[str, str | None]:
    raise SilentReconnectAuthRequired(
        "unexpected callback during silent reconnect",
    )


def _exception_chain_contains(
    exc: BaseException,
    needle: type[BaseException],
) -> bool:
    """Walk the full exception chain — ``ExceptionGroup`` sub-
    exceptions, ``__cause__``, ``__context__`` — looking for an
    instance of ``needle``.

    Necessary because the MCP SDK wraps everything in
    ``anyio.create_task_group``, which surfaces failures as
    ``ExceptionGroup`` instances. A simple ``isinstance`` on the
    top-level exception misses the actual cause buried inside.
    """
    if isinstance(exc, needle):
        return True
    subs: tuple[BaseException, ...] = getattr(exc, "exceptions", ())
    for sub in subs:
        if _exception_chain_contains(sub, needle):
            return True
    if exc.__cause__ is not None and _exception_chain_contains(
        exc.__cause__, needle,
    ):
        return True
    if exc.__context__ is not None and _exception_chain_contains(
        exc.__context__, needle,
    ):
        return True
    return False


def _synthesize_silent_reconnect_signature() -> RefreshFailureSignature:
    """When ``_noop_callback`` fired, no refresh response was ever
    received — but the SDK's behavior is itself proof that the
    bearer is dead AND refresh wouldn't help. Map this case to
    ``invalid_grant`` so §5.1 deletes the token immediately and
    §5.2's email pipeline notifies the user, instead of accumulating
    five identical "transient" failures over half an hour.
    """
    return RefreshFailureSignature(
        status_code=0,
        body_excerpt=(
            "synthesized: SDK fell into authorization_code grant "
            "during silent reconnect — stored bearer rejected by "
            "upstream, refresh_token grant was not attempted by the "
            "SDK (mcp/client/auth/oauth2.py:597), so no real refresh "
            "response is available to forensic-capture"
        ),
        error_code="invalid_grant",
        timestamp=datetime.now(UTC),
    )


async def _persist_post_reconnect_state(
    connection_store: ConnectionStore,
    oauth_auth: OAuthClientProvider,
    storage: McpTokenStorage,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
) -> None:
    """Apply the success-path housekeeping after a silent reconnect.

    Mirrors ``_classify_reconnect_failure`` on the happy side: clears
    the per-upstream connection-error row, resets the per-user
    consecutive-failure counter, and lowers the "user has been
    notified" flag so the next failure burst can re-arm the email
    pipeline.

    Also captures any freshly discovered OAuth server metadata. This
    matters when the reconnect happens to hit the SDK's 401-recovery
    branch — rare, but possible when the bearer is dead AND refresh
    only succeeds after a re-discovery — in which case the SDK
    populated ``provider.context.oauth_metadata`` from a fresh
    well-known fetch. Persisting it here means the *next* refresh
    skips discovery entirely (§3.8 — saves a round-trip on every
    later refresh until the upstream rotates its endpoints).
    """
    logger.info(
        "upstream.reconnect.stored_tokens.success",
        upstream_id=upstream.id,
        user=effective_user,
        org_id=org_id,
    )
    await connection_store.clear_connection_error(org_id, upstream.id)
    await connection_store.reset_refresh_failures(
        org_id, upstream.id, effective_user,
    )
    await connection_store.clear_notified(
        org_id, upstream.id, effective_user,
    )
    await _persist_discovered_oauth_metadata(oauth_auth, storage)


async def _classify_reconnect_failure(
    exc: BaseException,
    oauth_auth: OAuthClientProvider,
    connection_store: ConnectionStore,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
) -> DisconnectReason:
    """Classify a Step-3 connect failure during silent reconnect.

    The Step-3 ``connect_upstream_for_user`` may fail for many
    reasons: a refresh-rejected ``invalid_grant`` from the upstream
    (the user revoked / the token rotated), a transient 5xx, an SDK
    fall-through into authorization_code grant after a 401 (zombie
    bearer + missing refresh response), or a plain network blip.

    This helper folds four formerly-interleaved concerns into one
    place so the answer to "what triggers ``tokens_deleted``?" is one
    grep:

    1. Extract the refresh-failure signature, if the SDK captured one.
    2. Synthesize an ``invalid_grant`` signature when the SDK fell
       into authorization_code grant during a silent reconnect (see
       ``_noop_callback`` / ``SilentReconnectAuthRequired``).
    3. Persist the signature on both the per-user failure-counter row
       (for §5.1's threshold logic) and the per-upstream
       connection-error row (for the admin UI / dashboards).
    4. Run §5.1's delete-vs-keep policy and emit the matching
       structured log line (``tokens_deleted`` with ``reason`` /
       ``tokens_kept`` with the threshold context).

    The shape of the structured log fields is part of the
    operator-visible contract pinned by the
    ``test_tokens_kept_log_emits_threshold_fields`` /
    ``test_tokens_deleted_log_emits_reason_*`` tests in
    ``test_refresh_failure_policy.py``.

    Always returns ``token_refresh_failed`` — the disconnect reason
    is shared by every Step-3 failure path; the discriminator lives
    in the structured logs and the persisted signature, not in the
    return value.
    """
    signature = _extract_refresh_failure(oauth_auth)

    # If the SDK's 401 handler fell into authorization_code grant
    # (our ``_noop_callback`` raised), the bearer is dead AND the
    # SDK never even attempted ``refresh_token`` (per
    # ``mcp/client/auth/oauth2.py:597``). No refresh response was
    # received → ``signature`` is None. Synthesize an
    # ``invalid_grant`` so §5.1 deletes immediately and §5.2's
    # email pipeline notifies — instead of accumulating five
    # identical "transient" failures over half an hour while the
    # user wonders what's wrong.
    if signature is None and _exception_chain_contains(
        exc, SilentReconnectAuthRequired,
    ):
        signature = _synthesize_silent_reconnect_signature()
        logger.info(
            "upstream.reconnect.synthesized_invalid_grant",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            cause=(
                "SDK fell into authorization_code grant during "
                "silent reconnect"
            ),
        )

    if signature is not None:
        logger.info(
            "upstream.token.refresh.rejected",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            status_code=signature.status_code,
            error_code=signature.error_code,
            body_excerpt=signature.body_excerpt,
        )

    failure_count, first_at = await connection_store.record_refresh_failure(
        org_id, upstream.id, effective_user,
        signature=signature.to_dict() if signature else None,
    )

    await connection_store.set_connection_error(
        org_id, upstream.id, DisconnectReason.token_refresh_failed,
        signature=signature.to_dict() if signature else None,
    )

    if _should_delete_on_refresh_failure(
        signature, failure_count, first_at,
    ):
        reason = (
            "invalid_grant"
            if signature is not None and signature.error_code == "invalid_grant"
            else f"transient-threshold (failures={failure_count})"
        )
        logger.info(
            "upstream.reconnect.tokens_deleted",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            reason=reason,
            exc_info=True,
        )
        await connection_store.delete_user_token(
            org_id, effective_user, upstream.id,
        )
        await connection_store.reset_refresh_failures(
            org_id, upstream.id, effective_user,
        )
    else:
        elapsed = int((datetime.now(UTC) - first_at).total_seconds())
        logger.info(
            "upstream.reconnect.tokens_kept",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            failure_count=failure_count,
            elapsed_seconds=elapsed,
            threshold_count=MAX_CONSECUTIVE_TRANSIENT_FAILURES,
            threshold_window_seconds=MIN_TRANSIENT_FAILURE_WINDOW_SECONDS,
            exc_info=True,
        )
    return DisconnectReason.token_refresh_failed


async def _trigger_silent_refresh(
    oauth_auth: OAuthClientProvider, url: str,
) -> None:
    """Trigger a token-refresh side-effect via a lightweight HTTP GET.

    The SDK's ``OAuthClientProvider`` wraps every outgoing request: if
    the access token is within the refresh margin (or already expired
    but refresh-eligible) it transparently refreshes before forwarding.
    By the time control returns here the storage has been updated, so
    the caller can re-read the token row to see whether refresh
    actually produced a usable bearer.

    The request itself is *expected to fail*. MCP servers don't serve
    plain HTTP — they speak the streamable-HTTP / SSE transport — so
    the GET typically returns a non-200 or trips a transport error.
    That's fine; we only care about the auth-side-effect that has
    already happened. The bare ``except`` swallows the transport-level
    failure rather than masking a refresh problem (the refresh outcome
    is observed by the caller via ``storage.get_tokens()``).
    """
    try:
        async with httpx.AsyncClient(
            auth=oauth_auth,
            event_hooks={"request": [_inject_accept_json]},
            transport=SafeAsyncHTTPTransport(),
        ) as client:
            await asyncio.wait_for(client.get(url), timeout=10)
    except Exception:
        pass


async def _handle_token_read_failure(
    connection_store: ConnectionStore,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
) -> DisconnectReason:
    """Handle ``storage.get_tokens()`` raising during reconnect.

    Most commonly a decryption failure after an encryption-key / HKDF
    rotation (see ``FieldEncryptor``). Without the plaintext token we
    can't refresh, so we surface a clean ``token_refresh_failed`` so
    the admin UI can prompt for re-auth.

    Critically: do NOT delete the ciphertext. If this is a key-config
    mistake (wrong ``MCPOLIS_ENCRYPTION_KEY``, HKDF salt drift) wiping
    the token row would turn a recoverable misconfiguration into
    permanent data loss for every user on every upstream. The fix is
    to restore the right key; the row then decrypts on the next read.
    """
    logger.exception(
        "upstream.token.read_failed",
        upstream_id=upstream.id,
        user=effective_user,
        org_id=org_id,
    )
    await connection_store.set_connection_error(
        org_id, upstream.id, DisconnectReason.token_refresh_failed,
    )
    return DisconnectReason.token_refresh_failed


async def reconnect_with_stored_tokens(
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore,
    client_manager: UpstreamClientManager,
    server_url: str,
    timeout: float = 15,
) -> DisconnectReason | None:
    """Try to reconnect using stored tokens (no browser interaction).

    First triggers a lightweight HTTP request to refresh expired tokens,
    then creates the real MCP session.
    Returns None if connected, or a DisconnectReason if not.
    """
    if upstream.http is None:
        return DisconnectReason.no_tokens

    storage = McpTokenStorage(
        connection_store, org_id, upstream.id, effective_user,
        refresh_margin_seconds=TOKEN_REFRESH_MARGIN,
    )
    try:
        tokens = await storage.get_tokens()
    except Exception:
        return await _handle_token_read_failure(
            connection_store, org_id, upstream, effective_user,
        )
    if tokens is None:
        return DisconnectReason.no_tokens

    # Check if access token is expired
    raw_token = await connection_store.get_user_token(
        org_id, effective_user, upstream.id
    )
    access_expired = (
        raw_token is not None
        and raw_token.expires_at is not None
        and raw_token.expires_at < datetime.now(UTC)
    )

    oauth_auth = await _build_oauth_provider(
        upstream, storage,
        _noop_redirect, _noop_callback,
        server_url,
    )

    # Step 1: trigger a silent token refresh as a side-effect of an
    # auth-enabled HTTP request through the SDK's OAuthClientProvider.
    await _trigger_silent_refresh(oauth_auth, upstream.http.url)

    # Step 2: Check if we still have tokens
    refreshed_tokens = await storage.get_tokens()
    if refreshed_tokens is None:
        logger.info(
            "upstream.token.refresh.no_tokens",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            access_expired=access_expired,
        )
        if access_expired:
            return DisconnectReason.token_refresh_failed
        return DisconnectReason.token_expired

    # Step 3: Connect the real MCP session
    try:
        await asyncio.wait_for(
            client_manager.connect_upstream_for_user(
                upstream, effective_user, auth=oauth_auth
            ),
            timeout=timeout,
        )
        await _persist_post_reconnect_state(
            connection_store, oauth_auth, storage,
            org_id, upstream, effective_user,
        )
        return None  # Success
    except TimeoutError:
        logger.info(
            "upstream.reconnect.timeout",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            timeout_seconds=timeout,
        )
        await connection_store.set_connection_error(
            org_id, upstream.id, DisconnectReason.connection_timeout
        )
        return DisconnectReason.connection_timeout
    except Exception as exc:
        return await _classify_reconnect_failure(
            exc, oauth_auth, connection_store,
            org_id, upstream, effective_user,
        )


class SessionUnavailable(Exception):
    """No live session could be acquired without interactive auth.

    ``reason`` is a :class:`DisconnectReason` when an OAuth reconnect
    failed, else a short token (``"connect_failed"``, ``"no_session"``,
    ``"oauth_not_configured"``). Callers translate it into their own
    user-facing surface (a ``CallToolResult`` error for the tool router,
    an error banner + popup for the dashboard refresh endpoint).
    """

    def __init__(self, reason: "DisconnectReason | str") -> None:
        self.reason = reason
        super().__init__(str(reason))


async def acquire_upstream_session(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore | None,
    client_manager: UpstreamClientManager,
    server_url: str,
    timeout: float = 15,
) -> ClientSession:
    """Acquire a live MCP session, reattaching in-band — never a browser.

    The single source of truth for "make this upstream usable right now",
    shared by the tool router (per tool call) and the dashboard
    tool-refresh endpoint, so both paths reattach identically:

    - ``service_account``: lazily (re)open the shared session
      (DEFERRED_ATTACH → LIVE); idempotent when already LIVE.
    - OAuth: reuse ``effective_user``'s live session, else reconnect it
      from stored tokens (transparent token refresh). ``effective_user``
      is the calling user for ``per_user_oauth`` and the slot owner for
      ``admin_oauth``; the caller resolves it. Ignored for
      ``service_account``.

    Raises :class:`SessionUnavailable` when no session can be obtained
    without interactive sign-in.
    """
    # Local import mirrors the existing deferral in this module
    # (reconnect_all_oauth_upstreams) to avoid a policy↔service cycle.
    from mcpolis.domain.model.policy import AuthMode

    if upstream.auth.mode == AuthMode.service_account:
        try:
            await client_manager.ensure_shared_connected(upstream)
        except Exception as exc:
            raise SessionUnavailable("connect_failed") from exc
        try:
            return client_manager.get_session(upstream.id)
        except KeyError as exc:
            raise SessionUnavailable("no_session") from exc

    if connection_store is None:
        raise SessionUnavailable("oauth_not_configured")

    if client_manager.has_user_session(upstream.id, effective_user):
        return client_manager.get_session(
            upstream.id, user_id=effective_user,
        )

    reason = await reconnect_with_stored_tokens(
        org_id=org_id,
        upstream=upstream,
        effective_user=effective_user,
        connection_store=connection_store,
        client_manager=client_manager,
        server_url=server_url,
        timeout=timeout,
    )
    if reason is None:
        return client_manager.get_session(
            upstream.id, user_id=effective_user,
        )
    raise SessionUnavailable(reason)


async def acquire_and_refresh_with_recovery(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore | None,
    client_manager: UpstreamClientManager,
    tool_registry: ToolRegistry,
    server_url: str,
    max_attempts: int = 2,
) -> list[DiscoveredTool]:
    """Acquire the upstream's session and refresh its catalogue, retrying
    on a transport stall by reconnecting on a FRESH session.

    This is the recovery layer for E2B's intermittent post-reattach
    stdout stall (a ``commands.connect`` resume that delivers a response
    or two then goes silent). ``refresh_upstream`` raises a transport
    stall (timeout / connection-closed / broken stream) instead of
    persisting a half-empty catalogue; here we drop the stalled session,
    reconnect fresh (``reconnect_shared_fresh`` for service_account —
    drops the live ref so the sandbox service fresh-creates rather than
    reattaching to the same flaky sandbox), and retry. The operator gets
    a complete tool/resource/prompt list instead of a partial one or a
    30s hang.

    Non-stall failures (OAuth not configured, genuine server errors)
    propagate immediately — only a transport stall is retried, and only
    ``max_attempts`` times.
    """
    from mcpolis.domain.model.policy import AuthMode

    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        await acquire_upstream_session(
            org_id=org_id,
            upstream=upstream,
            effective_user=effective_user,
            connection_store=connection_store,
            client_manager=client_manager,
            server_url=server_url,
        )
        try:
            return await tool_registry.refresh_upstream(upstream.id)
        except Exception as exc:
            last_exc = exc
            is_last = attempt + 1 >= max_attempts
            if not is_transport_stall(exc) or is_last:
                raise
            logger.warning(
                "upstream.refresh.transport_stall_retry",
                upstream_id=upstream.id,
                org_id=org_id,
                attempt=attempt,
                error=str(exc) or exc.__class__.__name__,
            )
            # Force a fresh transport for the next attempt. Only the
            # shared (service_account) session rides the flaky E2B
            # reattach; for OAuth the next ``acquire_upstream_session``
            # above re-establishes the user session on its own.
            if upstream.auth.mode == AuthMode.service_account:
                await client_manager.reconnect_shared_fresh(upstream)
    # Loop always returns or raises; this satisfies the type checker.
    assert last_exc is not None
    raise last_exc


# Minimum time the "Fetching info" pill stays on screen after a connect.
# Without a floor, a warm-cache refresh can finish in <2s and the pill
# briefly flashes — which reads as a bug rather than progress.
_MIN_REFRESHING_DISPLAY_SECONDS = 4.0


async def _refresh_upstream_in_background(
    tool_registry: ToolRegistry, upstream_id: str,
    on_refreshed: Callable[[], None] | None = None,
) -> None:
    """Refresh an upstream's tool catalog and clear the refreshing flag.

    Caller is responsible for setting ``mark_refreshing`` synchronously
    BEFORE scheduling this task — that way the flag is observable to
    the very next ``GET /api/admin/upstreams`` even if the task body
    hasn't yet been picked up by the event loop. ``unmark_refreshing``
    happens here, in a ``finally``, so a timeout / error never leaves
    the UI stuck on "Fetching info".

    Honors ``_MIN_REFRESHING_DISPLAY_SECONDS`` so the dashboard pill
    has a chance to actually be seen — fast refreshes would otherwise
    flash and look like a glitch.
    """
    started_at = tool_registry.refreshing_started_at(upstream_id)
    try:
        await tool_registry.refresh_upstream(upstream_id)
    except Exception:
        logger.exception(
            "upstream.refresh.background_failed", upstream_id=upstream_id,
        )
    finally:
        if started_at is not None:
            elapsed = time.monotonic() - started_at
            remaining = _MIN_REFRESHING_DISPLAY_SECONDS - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        tool_registry.unmark_refreshing(upstream_id)
    if on_refreshed is not None:
        try:
            on_refreshed()
        except Exception:
            logger.exception(
                "upstream.refresh.notify_failed", upstream_id=upstream_id,
            )


async def connect_and_refresh_tools(
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore,
    auth_coordinator: PendingAuthCoordinator,
    client_manager: UpstreamClientManager,
    tool_registry: ToolRegistry,
    server_url: str,
    on_tokens_acquired: Callable[[], None] | None = None,
    on_error: Callable[[str, OAuthFailureReason], None] | None = None,
    on_tools_refreshed: Callable[[], None] | None = None,
) -> OAuthConnectResult:
    """Initiate OAuth connection; refresh the tool catalog in the background.

    ``mark_refreshing`` is flipped synchronously up front, so the
    dashboard's "Fetching info" pill is observable for the entire
    duration of the slow path — both the MCP session establishment
    inside ``initiate_oauth_connection`` and the catalog refresh that
    follows. The flag is cleared on early-exit branches (pending OAuth
    redirect / error) and otherwise lives until the background catalog
    task finishes. ``on_tools_refreshed`` fires once that task is done.
    """
    tool_registry.mark_refreshing(upstream.id)
    try:
        result = await initiate_oauth_connection(
            org_id=org_id,
            upstream=upstream,
            effective_user=effective_user,
            connection_store=connection_store,
            auth_coordinator=auth_coordinator,
            client_manager=client_manager,
            server_url=server_url,
            on_tokens_acquired=on_tokens_acquired,
            on_error=on_error,
        )
    except BaseException:
        tool_registry.unmark_refreshing(upstream.id)
        raise
    if result.connected:
        # Background task takes ownership of the flag; unmarks on done.
        asyncio.create_task(
            _refresh_upstream_in_background(
                tool_registry, upstream.id, on_tools_refreshed,
            )
        )
    else:
        # Pending (auth URL returned) or error: clear so the pill
        # doesn't stick. The follow-up second connect call will
        # mark again.
        tool_registry.unmark_refreshing(upstream.id)
    return result


async def reconnect_all_oauth_upstreams(
    org_id: str,
    upstreams: list[UpstreamDefinition],
    connection_store: ConnectionStore,
    client_manager: UpstreamClientManager,
    tool_registry: ToolRegistry,
    server_url: str,
    admin_emails: list[str] | None = None,
) -> dict[str, DisconnectReason]:
    """Try to reconnect ``admin_oauth`` upstreams using stored tokens.

    Phase 3: only ``admin_oauth`` upstreams are pre-warmed at startup;
    ``per_user_oauth`` reconnects lazily on each user's first request
    (one stored row per real user, eagerly reconnecting them all
    would be wasteful on cold start). With the tool catalog persisted
    via ``ToolRegistry.hydrate`` (Phase 0) the cold-start UI does not
    depend on this sweep — it only saves the first invocation a
    handshake.

    For ``admin_oauth`` the function tries each admin email's stored
    token in turn (so an org with several admins is resilient to one
    of them having a stale row). Refreshes the tool registry on the
    first successful reconnect per upstream.

    Runs all upstream reconnects in parallel. Returns a dict of
    upstream_id → ``DisconnectReason`` for upstreams that could not
    be reconnected.
    """
    from mcpolis.domain.model.policy import AuthMode

    reasons: dict[str, DisconnectReason] = {}
    candidate_users: list[str] = list(admin_emails or [])

    async def _try_one(upstream: UpstreamDefinition) -> None:
        worst_real_reason: DisconnectReason | None = None
        for user_id in candidate_users:
            reason = await reconnect_with_stored_tokens(
                org_id, upstream, user_id,
                connection_store, client_manager, server_url,
            )
            if reason is None:
                # Session reconnected. The boot-time tool refresh is
                # best-effort: the catalog was already hydrated from
                # persistence in Phase 0 and ``connect_runtime`` runs a
                # ``refresh_all()`` immediately after this sweep. So a
                # slow / stalled ``list_tools`` here (e.g. a remote MCP
                # taking >LIST_TOOLS_TIMEOUT to answer ``tools/list`` on
                # cold start) must NOT escape: letting it bubble to
                # ``connect_runtime``'s catch-all mislabels one
                # upstream's transient stall as a whole-org
                # ``org.runtime.startup.failed`` (an ERROR → Sentry
                # alert) and, via the gather below, cancels the sibling
                # admin_oauth reconnects. Swallow at warning level —
                # visible in logs, a Sentry breadcrumb, not an event.
                try:
                    await tool_registry.refresh_upstream(upstream.id)
                except Exception:
                    logger.warning(
                        "upstream.reconnect.refresh_failed",
                        org_id=org_id,
                        upstream_id=upstream.id,
                        exc_info=True,
                    )
                return
            if reason != DisconnectReason.no_tokens:
                # Remember the most recent real failure but keep
                # trying other admins in case one of them is healthy.
                worst_real_reason = reason
        if worst_real_reason is not None:
            reasons[upstream.id] = worst_real_reason

    tasks = [
        _try_one(u) for u in upstreams
        if u.auth.mode == AuthMode.admin_oauth
    ]
    if tasks:
        # ``return_exceptions=True`` so one upstream's reconnect
        # surfacing an unexpected raise can't cancel its healthy
        # siblings mid-flight. ``_try_one`` is self-contained now, but
        # this mirrors the Phase-1 connect sweep in
        # ``OrgRuntimeManager.connect_runtime`` and is belt-and-braces
        # against ``reconnect_with_stored_tokens`` surprising us.
        await asyncio.gather(*tasks, return_exceptions=True)
    return reasons


# Periodic refresh moved to ``oauth_refresh.py`` during the
# post-§5.2 cleanup. Re-exports below preserve the legacy
# ``upstream_connection_service.refresh_token_for_user`` import
# path; new code should import from ``oauth_refresh`` directly.
from mcpolis.domain.services.oauth_refresh import (  # noqa: E402
    TOKEN_REFRESH_INTERVAL,
    TOKEN_REFRESH_MARGIN,
    TOKEN_REFRESH_MAX_RETRIES,
    TOKEN_REFRESH_RETRY_DELAY,
    periodic_token_refresh,
    refresh_token_for_user,
)
# §5.5 liveness probe moved to ``oauth_liveness.py`` during the
# post-§5.2 cleanup. Symbols re-exported here so existing callers
# (and tests that monkeypatch ``upstream_connection_service.*``)
# keep working — new code should import from ``oauth_liveness``
# directly.
from mcpolis.domain.services.oauth_liveness import (  # noqa: E402
    HEALTH_CHECK_INTERVAL,
    HEALTH_CHECK_PROBE_TIMEOUT,
    ProbeOutcome,
    probe_upstream_liveness,
    run_liveness_probes_for_org,
)
from mcpolis.domain.services.oauth_liveness import _summarize_probe_outcomes  # noqa: E402  # pyright: ignore[reportPrivateUsage]

__all__ = [
    # ``oauth_liveness`` re-exports (legacy import path; prefer
    # importing from ``oauth_liveness`` in new code)
    "HEALTH_CHECK_INTERVAL",
    "HEALTH_CHECK_PROBE_TIMEOUT",
    "ProbeOutcome",
    "_summarize_probe_outcomes",
    "probe_upstream_liveness",
    "run_liveness_probes_for_org",
    # Public surface of this module
    "DisconnectReason",
    "OAuthConnectResult",
    "RefreshFailureSignature",
    "REFRESH_FAILURE_BODY_LIMIT",
    "MAX_CONSECUTIVE_TRANSIENT_FAILURES",
    "MIN_TRANSIENT_FAILURE_WINDOW_SECONDS",
    "TOKEN_REFRESH_INTERVAL",
    "TOKEN_REFRESH_MARGIN",
    "TOKEN_REFRESH_MAX_RETRIES",
    "TOKEN_REFRESH_RETRY_DELAY",
    "try_connect_with_stored_tokens",
    "initiate_oauth_connection",
    "reconnect_with_stored_tokens",
    "connect_and_refresh_tools",
    "reconnect_all_oauth_upstreams",
    "refresh_token_for_user",
    "periodic_token_refresh",
]
