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
from collections.abc import Awaitable, Callable, Coroutine
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
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_token_storage import (
    NO_ROW,
    LoadedRevision,
    McpTokenStorage,
)
from mcpolis.adapters.auth.pending_auth import (
    PendingAuth,
    PendingAuthCoordinator,
    UpstreamUnreachableError,
)
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import (
    ForcedRefreshKey,
    OpenUserSession,
    UpstreamClientManager,
    UpstreamStopped,
)
from mcpolis.adapters.upstream_clients.safe_http_transport import (
    SafeAsyncHTTPTransport,
)
from mcpolis.adapters.upstream_clients.session_single_flight import (
    ConnectAborted,
)
from mcpolis.domain.model.oauth_errors import TERMINAL_AUTH_ERROR_CODES
from mcpolis.domain.model.upstream import DiscoveredTool, UpstreamDefinition
from mcpolis.domain.services.background_tasks import BackgroundTaskSet
from mcpolis.domain.services.backoff import Backoff
from mcpolis.domain.services.cancel_shield import finish_despite_cancels
from mcpolis.domain.services.sign_in_refresh_lock import SignInRefreshLock
from mcpolis.domain.services.tool_registry import (
    ToolRegistry,
    is_transport_stall,
)
from mcpolis.domain.services.upstream_health_check import SignInWarner

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Background jobs this module starts (a tool-catalog refresh, a token
# refresh), held until each ends. Callers may drop the tasks these
# functions return.
_background_tasks = BackgroundTaskSet()
# Sign-ins waiting for their browser step. A shutdown cancels them at
# once rather than waiting: the browser's callback is refused while the
# app drains, so none of them can finish.
_sign_in_waits = BackgroundTaskSet(cancel_at_shutdown=True)


async def _inject_accept_json(request: httpx.Request) -> None:
    """Ensure Accept: application/json on all requests including auth-flow.

    GitHub's token endpoint returns form-encoded data by default;
    this header makes it return JSON that the MCP SDK can parse.
    Workaround for: https://github.com/modelcontextprotocol/typescript-sdk/issues/759
    """
    request.headers.setdefault("Accept", "application/json")


# Streamable-HTTP MCP servers require BOTH media types on the probe
# request; a bare ``application/json`` is rejected by spec-strict
# servers before the auth layer ever runs.
_MCP_PROBE_ACCEPT = "application/json, text/event-stream"


def _auth_probe_body() -> dict[str, Any]:
    """Minimal MCP ``initialize`` payload, used only to provoke a 401."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcpolis", "version": "1"},
        },
    }


async def probe_upstream_for_auth(
    url: str,
    oauth_auth: httpx.Auth,
    *,
    timeout: float | None = None,
) -> None:
    """Poke an upstream so the SDK's ``OAuthClientProvider`` runs.

    Every OAuth side-effect we need — fresh consent, silent refresh,
    401 recovery — is driven by ``OAuthClientProvider`` wrapping an
    outgoing request. The response is irrelevant and routinely an
    error; only the auth-side-effect matters.

    The request MUST be a ``POST`` carrying an MCP ``initialize``
    body. This used to be a bare ``GET``, which silently broke fresh
    consent against any server that does not route ``GET`` on its MCP
    path: ``mcp.mixpanel.com`` answers ``405 Allow: POST, DELETE``,
    and the SDK only enters its authorization flow on a ``401``. No
    401 meant no discovery, no DCR, no redirect URL — the caller then
    waited out its full deadline and reported the upstream as
    unreachable, when in fact the server was healthy and merely
    refusing the verb. See ``_start_background_token_acquisition``.

    Callers are expected to swallow transport errors; ``timeout``
    bounds the call for the refresh paths, which must not hang the
    periodic sweep.
    """
    async with httpx.AsyncClient(
        auth=oauth_auth,
        event_hooks={"request": [_inject_accept_json]},
        transport=SafeAsyncHTTPTransport(),
    ) as client:
        # Explicit Accept wins over ``_inject_accept_json``'s
        # ``setdefault``; the hook still supplies plain JSON on the
        # auth-flow sub-requests (token endpoint / DCR), which is the
        # GitHub form-encoding workaround it exists for.
        request = client.post(
            url,
            json=_auth_probe_body(),
            headers={"Accept": _MCP_PROBE_ACCEPT},
        )
        if timeout is None:
            await request
        else:
            await asyncio.wait_for(request, timeout=timeout)


def _coerce_failure_reason(reason: str | None) -> OAuthFailureReason:
    """Map a stringly-typed failure reason back to the enum.

    ``PendingAuth`` carries the reason as a plain string (it is an
    adapter and must not import this module's enum). Coercion happens
    inside an ``except`` handler, so an unrecognised value must not
    raise: a ``ValueError`` there would turn a handled auth failure
    into a 500 on the connect endpoint.
    """
    if reason is None:
        return OAuthFailureReason.upstream_unavailable
    try:
        return OAuthFailureReason(reason)
    except ValueError:
        logger.warning(
            "upstream.oauth.failure_reason.unrecognised", reason=reason,
        )
        return OAuthFailureReason.unknown


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
        error_reported: bool = False,
        aborted: bool = False,
    ) -> None:
        self.connected = connected
        self.authorization_url = authorization_url
        self.error = error
        self.failure_reason = failure_reason
        # A Stop cut the connect short: not a failure to show or alert on.
        self.aborted = aborted
        # True when the background token-acquisition task already ran
        # the caller's ``on_error`` for this failure. The connect route
        # emits its own ``upstream_oauth_failed`` for synchronous
        # failures, so without this flag any failure that BOTH signals
        # the foreground (``mark_failed``) and calls ``on_error`` is
        # counted twice on the failure dashboard.
        self.error_reported = error_reported


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

    And: reload the stored tokens before refreshing an expired copy (see
    ``_initialized``).
    """

    last_refresh_failure: RefreshFailureSignature | None = None
    # The stored tokens (their revision) whose refresh was rejected. The
    # failure is about those tokens only: by the time it is acted on, the
    # user may have signed in again, or another refresh saved newer ones.
    last_refresh_failure_revision: LoadedRevision = NO_ROW

    # The SDK loads the stored tokens once, then keeps its own copy. Its
    # only reload hook is this flag, read at the start of every request.
    # It reads False while the copy has expired, so the SDK reloads the
    # stored tokens before it refreshes: another holder of the same
    # sign-in (the background refresh, a reconnect) may have renewed them
    # already, which used up this copy's refresh token. Refreshing with it
    # anyway is rejected by upstreams that rotate refresh tokens, and ones
    # with reuse detection revoke the whole sign-in.
    _loaded_once = False

    @property
    def _initialized(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._loaded_once and self.context.is_token_valid()

    @_initialized.setter
    def _initialized(self, value: bool) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        self._loaded_once = value

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
            storage = self.context.storage
            if isinstance(storage, McpTokenStorage):
                self.last_refresh_failure_revision = storage.loaded_revision
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


def failure_revision(
    provider: OAuthClientProvider, storage: McpTokenStorage,
) -> LoadedRevision:
    """Which stored tokens a failed refresh or reconnect is about: the
    ones whose refresh was rejected, if one was; else the ones the
    connect used.

    They differ when the user signed in again meanwhile. The rejected
    refresh (of the OLD sign-in) is remembered on the provider, while the
    connect afterwards reloads the NEW one: blaming the new sign-in for
    the old one's rejection deleted it.
    """
    if (
        isinstance(provider, _InitializingOAuthClientProvider)
        and provider.last_refresh_failure is not None
        and provider.last_refresh_failure_revision is not NO_ROW
    ):
        return provider.last_refresh_failure_revision
    return storage.loaded_revision


async def tokens_are_still_stored(
    connection_store: ConnectionStore,
    org_id: str,
    upstream_id: str,
    user_id: str,
    revision: LoadedRevision,
) -> bool:
    """Whether the stored tokens are still the ones read with ``revision``:
    nobody signed in again or disconnected since, and no other refresh
    saved newer tokens. Only then does their failure say anything about
    what is stored."""
    if revision is NO_ROW:
        return False
    stored = await connection_store.get_user_token(org_id, user_id, upstream_id)
    return stored is not None and stored.revision == revision


# §5.1 delete-vs-retry tuning knobs. Picked conservatively: five
# consecutive transient failures AND at least half an hour of sustained
# trouble before we wipe the user's token. A single bad minute of network
# can easily produce three failures back-to-back across the 10-min
# periodic interval; five forces a long enough sustained-outage signal
# that we've clearly left "transient" territory. The 30-min window keeps
# us from deleting after a brief multi-failure burst that then stopped.
MAX_CONSECUTIVE_TRANSIENT_FAILURES = 5
MIN_TRANSIENT_FAILURE_WINDOW_SECONDS = 30 * 60


# Step-3 connect cap for the gateway tool-call dead-token reconnect probe
# (``settle_oauth_state_after_stall``). Tighter than the default 15s
# reconnect budget: the dead-token case the probe targets fails fast, so
# this only bounds the rare transient-unreachable case, where a
# non-retry-safe caller would otherwise wait the full budget for an error
# whose outcome is already decided.
PROBE_RECONNECT_TIMEOUT = 8


async def purge_user_oauth_state(
    connection_store: ConnectionStore,
    org_id: str,
    upstream_id: str,
    user_id: str,
    *,
    revision: LoadedRevision,
) -> bool:
    """Tear down ALL per-user OAuth state for one (upstream, user) after a
    terminal auth rejection: the token, the DCR ``client_info``, the
    cached authorization-server ``oauth_metadata``, and the
    refresh-failure counter.

    Dropping ``client_info`` here is safe precisely because the token is
    deleted in the same breath. The hazard that
    ``_refresh_stale_client_info_for_consent`` guards against — a live
    ``refresh_token`` issued under the old ``client_id`` later failing
    with ``invalid_grant: Client ID mismatch`` — cannot apply once the
    token is gone. The next consent re-runs DCR and mints a fresh
    ``client_id``; that is the only escape from an ``invalid_client``
    brick the backend can trigger on its own (the upstream's 400 at the
    browser ``/oauth/authorize`` step is never observable here).

    Only while the stored tokens are still the ones read with
    ``revision``, the ones the rejection was about. A reconnect that
    started before the user signed in again must not delete the new
    sign-in, nor the app registration that sign-in uses; and newer tokens
    another refresh of the same sign-in saved meanwhile may still work.
    Returns whether anything was purged.
    """
    if revision is NO_ROW or not await connection_store.delete_user_token_if_current(
        org_id, user_id, upstream_id, expected_revision=revision,
    ):
        logger.info(
            "upstream.oauth.purge_skipped",
            upstream_id=upstream_id,
            user=user_id,
            org_id=org_id,
            reason="tokens_replaced",
        )
        return False
    await connection_store.delete_client_info(org_id, upstream_id, user_id)
    await connection_store.delete_oauth_metadata(org_id, upstream_id, user_id)
    await connection_store.reset_refresh_failures(org_id, upstream_id, user_id)
    return True


async def delete_refused_sign_in(
    connection_store: ConnectionStore,
    org_id: str,
    upstream: UpstreamDefinition,
    user_id: str,
    *,
    revision: LoadedRevision,
    warner: SignInWarner | None,
) -> bool:
    """Delete a sign-in the upstream refused, then warn whoever must sign
    in again (§5.2).

    The one funnel for this: the periodic refresh and a reconnect both
    delete refused sign-ins, and the reconnect used to do it with no
    email at all, signing members out silently. The email goes out only
    when ``purge_user_oauth_state`` actually deleted something, so a
    sign-in replaced meanwhile is kept and its owner is not told to sign
    in again. The email is only scheduled here (``warn_deleted`` sends
    it in the background and logs its own failures): the caller may be
    a shared reconnect that requests are waiting on. ``warner`` is None
    while health emails are off, and for deletions that are not a
    refusal (see ``_classify_reconnect_failure``).
    """
    purged = await purge_user_oauth_state(
        connection_store, org_id, upstream.id, user_id, revision=revision,
    )
    if not purged or warner is None:
        return purged
    if await connection_store.was_notified(org_id, upstream.id, user_id):
        # The hourly sweep already emailed about this very failure (it can
        # run between the failure being recorded and this delete). The
        # marker can't be stale: a fresh sign-in clears it
        # (``_mark_sign_in_working``).
        logger.info(
            "upstream.oauth.sign_in_deleted.already_warned",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
        )
        return purged
    warner.warn_deleted(org_id=org_id, upstream=upstream, user_id=user_id)
    return purged


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

    - A terminal auth code (``invalid_grant`` = dead refresh token, or
      ``invalid_client`` = dead DCR registration) → delete immediately.
      The upstream is telling us this credential is permanently
      rejected; keeping it around just means every subsequent tick
      re-discovers the same rejection and churns the §5.4 signature row.
      The caller pairs the delete with ``purge_user_oauth_state`` so an
      ``invalid_client`` also drops the dead client_info.
    - Any other failure (5xx, network, unknown) → only delete once we've
      accumulated ``MAX_CONSECUTIVE_TRANSIENT_FAILURES`` failures spanning
      at least ``MIN_TRANSIENT_FAILURE_WINDOW_SECONDS``. Below the
      threshold, keep the token and retry.

    The ``now`` parameter is for testability (inject a fixed clock) —
    callers in production should leave it ``None``.
    """
    if (
        signature is not None
        and signature.error_code in TERMINAL_AUTH_ERROR_CODES
    ):
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
    # NOTE: stale-client self-heal lives in
    # ``_refresh_stale_client_info_for_consent`` and is only invoked from
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
    existing_tokens = await storage.peek_tokens()
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
        # A deliberate Connect: rebuild on the stored tokens (after a
        # fresh sign-in, the NEW ones) rather than keep a session that may
        # carry the old sign-in.
        await client_manager.replace_user_session(
            upstream, effective_user, auth=oauth_auth,
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
        await client_manager.replace_user_session(
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
        detail = client_manager.hide_secrets_in_error(
            upstream.id, _unwrap_error(e),
        )
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
) -> bool:
    """Resume an in-flight OAuth flow from a callback code on disk.

    The OAuth callback endpoint persists the ``(code, state)`` pair
    keyed by ``(org_id, upstream_id, user_id)`` so the redirect can
    arrive even if the server restarts mid-flow. When the user clicks
    "Connect" again after the restart, this helper finds the stored
    code, pre-fills the ``PendingAuth`` so the SDK's
    ``callback_handler`` returns immediately, and pops the code from
    durable storage so it isn't replayed on a third attempt.

    Returns ``True`` if a stored code was resumed, ``False`` otherwise
    (the typical first-time-consent path; the SDK then waits on the
    browser redirect normally). The caller uses this to skip the
    client_info self-heal: a resumed code+PKCE was issued under the
    existing ``client_id``, so re-registering would orphan it.
    """
    stored_code = await connection_store.pop_pending_code(
        org_id, upstream.id, effective_user,
    )
    if stored_code is None:
        return False
    code, original_state = stored_code
    pending.complete(code, original_state)
    logger.info(
        "upstream.oauth.resume_with_stored_code",
        upstream_id=upstream.id,
        user=effective_user,
        org_id=org_id,
    )
    return True


async def _refresh_stale_client_info_for_consent(
    storage: McpTokenStorage,
    upstream: UpstreamDefinition,
    server_url: str,
    connection_store: ConnectionStore,
) -> None:
    """Self-heal a stale DCR registration on the consent path only.

    Two independent triggers drop the stored client so the SDK runs a
    fresh Dynamic Client Registration:

    **Trigger 1 — stale redirect URI.** If the gateway's callback path
    changed since the last registration (e.g. the 2026-05-07
    mcpolis.seniak.com → mcphero.io rebrand, or commit 4b43375 moving
    ``/oauth/upstream/callback`` under ``/api``), the upstream's
    authorize endpoint rejects redirect URIs it doesn't have on file.
    Always safe to drop.

    **Trigger 2 — possibly-dead client.** The upstream may have expired,
    rotated, or forgotten the ``client_id`` and now answers
    ``invalid_client`` at its ``/oauth/authorize`` step. That 400 is
    served to the user's *browser*, never to us — the backend only sees
    the callback never arrive (a ``callback_handler`` TimeoutError;
    Sentry MCPOLIS-BACKEND-2). With no observable signal to react to, we
    proactively re-register on consent whenever doing so cannot strand a
    live credential.

    Both triggers are safe ONLY on the fresh-consent path, never on a
    silent path (refresh / liveness probe). ``grant_type=refresh_token``
    carries no ``redirect_uri`` (RFC 6749 §6), so dropping a client that
    still has a live ``refresh_token`` makes the next refresh run under a
    freshly-DCR'd ``client_id`` the stored token wasn't issued under, and
    the upstream rejects with ``invalid_grant: Client ID mismatch``.
    Trigger 2 therefore fires only when no refresh token is at risk: no
    token row at all, or a stored token with no refresh_token (the mee6
    shape — a long-lived access token with no refresh grant). When a live
    refresh token IS present, only trigger 1 can drop.
    """
    if upstream.auth.client_id:
        # Pre-configured client (not DCR) — nothing to self-heal.
        return
    existing = await storage.get_client_info()
    if existing is None:
        return

    # Trigger 1: redirect-URI drift — always safe to drop.
    callback_url = (
        f"{server_url.rstrip('/')}/api/oauth/upstream/callback"
    )
    registered_uris = [str(u) for u in (existing.redirect_uris or [])]
    if callback_url not in registered_uris:
        logger.warning(
            "upstream.oauth.client_info.stale_redirect_dropped",
            upstream_id=upstream.id,
            registered_redirect_uris=registered_uris,
            current_callback_url=callback_url,
        )
        await storage.delete_client_info()
        return

    # Trigger 2: possibly-dead client — re-register only when no live
    # refresh token could be stranded by a new client_id.
    raw_token = await connection_store.get_user_token(
        storage.org_id, storage.user_id, storage.upstream_id,
    )
    if raw_token is None or not raw_token.refresh_token:
        logger.info(
            "upstream.oauth.client_info.reregister_on_consent",
            upstream_id=upstream.id,
            org_id=storage.org_id,
            user=storage.user_id,
            has_stored_token=raw_token is not None,
        )
        await storage.delete_client_info()


async def _drop_client_info_on_dead_client_failure(
    exc: BaseException,
    signature: RefreshFailureSignature | None,
    storage: McpTokenStorage,
    upstream: UpstreamDefinition,
) -> None:
    """Reactive consent-path backstop for a dead DCR client — the
    complement to the proactive ``_refresh_stale_client_info_for_consent``.

    Proactive self-heal deliberately leaves the stored client alone when a
    live refresh token is present (dropping it pre-flight could strand that
    token under a fresh ``client_id`` — see that function's docstring). So
    the one case it can't fix is "live refresh token, but the client is in
    fact dead." This runs after the consent flow has failed and recovers
    exactly that case, by dropping the client so the *next* consent
    re-registers. Triggers on:

    - an explicit ``invalid_client`` from the token endpoint (the SDK's
      refresh/exchange POST — unambiguous, the upstream rejected the
      ``client_id``), or
    - a callback ``TimeoutError`` (the browser-side authorize-step 400 has
      no server-visible response; it only shows up as the callback never
      arriving — Sentry MCPOLIS-BACKEND-2) when no live refresh token
      could be stranded by re-registering.

    No-op for pre-configured (non-DCR) clients and when nothing is stored.
    By the time we reach a callback timeout the SDK has already attempted
    any viable refresh, so an explicit ``invalid_client`` is the precise
    signal and the timeout is the coarse fallback.
    """
    if upstream.auth.client_id is not None:
        return
    if await storage.get_client_info() is None:
        return
    invalid_client = (
        signature is not None and signature.error_code == "invalid_client"
    )
    drop = invalid_client
    if not drop and _exception_chain_contains(exc, TimeoutError):
        tokens = await storage.peek_tokens()
        drop = tokens is None or not tokens.refresh_token
    if not drop:
        return
    logger.info(
        "upstream.oauth.client_info.dropped_on_consent_failure",
        upstream_id=upstream.id,
        org_id=storage.org_id,
        user=storage.user_id,
        reason="invalid_client" if invalid_client else "callback_timeout",
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
    sign_in_check: Callable[[], Awaitable[str | None]] | None = None,
) -> OAuthConnectResult:
    """Initiate an OAuth connection to an upstream MCP server.

    If stored tokens exist, connects directly (in the current task).
    If no tokens or connection fails, starts a background task that
    only acquires tokens (no MCP session), then returns the
    authorization URL.  The actual MCP session is created later by
    the caller's task after the user completes the OAuth flow.

    ``sign_in_check`` is asked again at the callback and right before the
    new sign-in is saved: why it may not land any more, or ``None``. An
    admin's Connect to an admin_oauth upstream passes one, refusing a
    sign-in once another admin took the upstream's admin sign-in slot
    meanwhile.
    """
    if upstream.http is None:
        return OAuthConnectResult(
            error="This MCP does not support authentication (not an HTTP server)."
        )
    if client_manager.is_stopped(upstream.id):
        # Signing in would end on a refused session: only an admin's
        # Start (which lifts the stop first) opens one again.
        return OAuthConnectResult(
            error="An administrator stopped this MCP. It is available "
            "again once they start it.",
        )

    # Try connecting with existing tokens first (no PendingAuth needed).
    result = await try_connect_with_stored_tokens(
        org_id, upstream, effective_user, connection_store,
        client_manager, server_url,
    )
    if result is not None:
        return result

    pending = auth_coordinator.create_pending(
        org_id, upstream.id, effective_user, sign_in_check=sign_in_check,
    )

    storage = McpTokenStorage(
        connection_store, org_id, upstream.id, effective_user,
        refresh_margin_seconds=TOKEN_REFRESH_MARGIN,
        # Removing the person from the org aborts this flow
        # (``PendingAuthCoordinator.abort_for_user``): a code that already
        # arrived must not save a sign-in for a non-member. Nor for an
        # admin whose sign-in ``sign_in_check`` now refuses.
        fresh_sign_in_refusal=pending.check_sign_in,
    )

    # Resume a callback code stashed before a mid-flow restart FIRST. That
    # code+PKCE was issued under the existing ``client_id``, so the
    # client_info self-heal below must not run when we're resuming — a
    # re-DCR would mint a new client_id and the token exchange would fail
    # on a client mismatch.
    resumed = await _resume_pending_from_stored_code(
        connection_store, pending, org_id, upstream, effective_user,
    )

    if not resumed:
        # Consent path: self-heal a stale DCR client_info before the SDK's
        # OAuth flow starts — stale redirect URI, or a possibly-dead client
        # when no live refresh token is at risk. Silent paths (refresh /
        # liveness) deliberately skip this; see
        # ``_refresh_stale_client_info_for_consent`` docstring.
        await _refresh_stale_client_info_for_consent(
            storage, upstream, server_url, connection_store,
        )

    async def callback_then_fresh_sign_in() -> tuple[str, str | None]:
        # The code returned here is exchanged for tokens next: a fresh
        # sign-in, which replaces whatever is stored.
        result = await pending.callback_handler()
        storage.mark_fresh_sign_in()
        return result

    oauth_auth = await _build_oauth_provider(
        upstream, storage,
        pending.redirect_handler,
        callback_then_fresh_sign_in,
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
        # on_error — surface the same message AND the same reason
        # synchronously, so the connect endpoint returns within seconds
        # instead of waiting the 30s discovery deadline. Falling back to
        # ``upstream_unavailable`` only when the background task didn't
        # say: blaming the network for an auth failure is what made the
        # Mixpanel report unreadable.
        auth_coordinator.cleanup(org_id, upstream.id, effective_user)
        return OAuthConnectResult(
            error=e.user_message,
            failure_reason=_coerce_failure_reason(e.reason),
            # Every path that reaches ``mark_failed`` has already called
            # ``on_error``; the route must not emit a second event.
            error_reported=True,
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
            # The response doesn't matter — the OAuthClientProvider
            # handles the 401 and runs the full OAuth flow as a
            # side-effect. The verb does matter; see
            # ``probe_upstream_for_auth``.
            await probe_upstream_for_auth(url, auth)
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
                pending.mark_failed(user_msg, reason.value)
                if on_error is not None:
                    on_error(user_msg, reason)
                return

            # Reactive dead-client backstop. The proactive consent
            # self-heal can't act when a live refresh token pinned the
            # client_info; if the flow then failed because that client is
            # in fact dead, drop it here so the next consent re-registers.
            await _drop_client_info_on_dead_client_failure(
                e, _extract_refresh_failure(auth), storage, upstream,
            )

        if pending.aborted:
            # Called off: the person was removed from the org. Nothing of
            # this sign-in may stay, and nobody is told it worked; a
            # Connect still waiting for its sign-in link is answered.
            await _forget_called_off_sign_in(storage)
            pending.mark_failed(
                "The sign-in was cancelled.",
                OAuthFailureReason.unknown.value,
            )
            return

        # If the redirect_handler was never called, tokens were
        # refreshed silently — signal this to the caller.
        if not pending.redirect_url:
            tokens = await storage.peek_tokens()
            if tokens is not None:
                pending.mark_tokens_refreshed()

        # Notify that tokens have been acquired (for SSE push),
        # or that the flow failed. A refused sign-in failed, whatever
        # sign-in of the person's was stored before it.
        refusal = pending.refusal or storage.fresh_sign_in_refused
        tokens = await storage.peek_tokens()
        if tokens is not None and refusal is None:
            # Capture the metadata the SDK discovered during this flow
            # (initial consent or 401 recovery) so the next process
            # boot's ``_build_oauth_provider`` can pre-populate
            # ``OAuthContext.oauth_metadata`` and the periodic refresh
            # branch hits the upstream's real ``token_endpoint``
            # instead of ``<base>/token``. See §3.8 / §5.4.
            await _persist_discovered_oauth_metadata(auth, storage)
            if storage.fresh_sign_in_saved:
                await _settle_fresh_sign_in(auth, storage)
            if pending.aborted:
                # Called off while these writes ran: the removal's
                # clean-up may have run before them, and they would leave
                # the removed person an app registration and server
                # metadata.
                await _forget_called_off_sign_in(storage)
                return
            if on_tokens_acquired is not None:
                on_tokens_acquired()
        else:
            # Terminal failure with no redirect and no tokens. Unblock
            # the foreground waiter with the REAL message: without this
            # ``mark_failed`` the connect endpoint rides its full
            # discovery deadline and then reports "Could not reach this
            # MCP server", blaming the network for what is almost always
            # an auth-flow failure against a perfectly healthy upstream.
            # (Mixpanel, 2026-09-18: 155 ms to fail here, 30 s to say so,
            # and the wrong cause.) A refused sign-in says why.
            message = refusal or "Authentication failed — please try again."
            pending.mark_failed(
                message, OAuthFailureReason.token_exchange.value,
            )
            if on_error is not None:
                on_error(message, OAuthFailureReason.token_exchange)

    return _sign_in_waits.spawn(_acquire_tokens())


async def _forget_called_off_sign_in(storage: McpTokenStorage) -> None:
    """Delete what a called-off sign-in (``PendingAuth.abort``: the
    person was removed from the org) may have saved for its upstream: the
    removal deletes everything the person has, but a write that was under
    way can land after it.

    Only this flow's own. Its tokens go only while they are the ones it
    saved, and nothing goes while another sign-in is stored: the person
    may have been invited again and signed in anew meanwhile, and deleting
    that sign-in, or the app registration and server metadata it uses,
    would sign them out without a word."""
    store = storage.connection_store
    org_id, upstream_id, user_id = (
        storage.org_id, storage.upstream_id, storage.user_id,
    )
    own_revision = storage.loaded_revision
    if storage.fresh_sign_in_saved and own_revision is not NO_ROW:
        await store.delete_user_token_if_current(
            org_id, user_id, upstream_id, expected_revision=own_revision,
        )
    if await store.get_user_token(org_id, user_id, upstream_id) is not None:
        logger.info(
            "upstream.oauth.called_off_sign_in.newer_sign_in_kept",
            upstream_id=upstream_id,
            user=user_id,
            org_id=org_id,
        )
        return
    await store.delete_client_info(org_id, upstream_id, user_id)
    await store.delete_oauth_metadata(org_id, upstream_id, user_id)
    await store.reset_refresh_failures(org_id, upstream_id, user_id)
    await store.clear_notified(org_id, upstream_id, user_id)
    logger.info(
        "upstream.oauth.called_off_sign_in.forgotten",
        upstream_id=upstream_id,
        user=user_id,
        org_id=org_id,
    )


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
    await _mark_sign_in_working(
        connection_store, org_id, upstream.id, effective_user,
    )
    await _persist_discovered_oauth_metadata(oauth_auth, storage)


async def _mark_sign_in_working(
    connection_store: ConnectionStore,
    org_id: str,
    upstream_id: str,
    user_id: str,
) -> None:
    """The user's sign-in just proved good: clear the upstream's error,
    the user's failed-refresh count (so earlier failures cannot add up to
    deleting it), and the "already emailed" marker (so a later failure
    notifies again)."""
    await connection_store.clear_connection_error(org_id, upstream_id)
    await connection_store.reset_refresh_failures(org_id, upstream_id, user_id)
    await connection_store.clear_notified(org_id, upstream_id, user_id)


async def _settle_fresh_sign_in(
    provider: OAuthClientProvider, storage: McpTokenStorage,
) -> None:
    """Bookkeeping after a fresh sign-in's tokens were saved.

    Its app registration is saved again: a reconnect of the old sign-in
    that failed while the user was on the consent page deletes it (the
    token it deleted was still the old one), and without it the new
    sign-in's first refresh fails. The failed-refresh count restarts,
    so the old sign-in's failures cannot add up to deleting this one.
    """
    client_info = provider.context.client_info
    if isinstance(client_info, OAuthClientInformationFull):
        await storage.set_client_info(client_info)
    await _mark_sign_in_working(
        storage.connection_store, storage.org_id,
        storage.upstream_id, storage.user_id,
    )


async def _classify_reconnect_failure(
    exc: BaseException,
    oauth_auth: OAuthClientProvider,
    storage: McpTokenStorage,
    connection_store: ConnectionStore,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    *,
    warner: SignInWarner | None,
    refresh_tried: bool = False,
) -> DisconnectReason:
    """Classify a Step-3 connect failure during silent reconnect.

    ``refresh_tried``: the refresh token was tried, by this reconnect
    (its own refresh saved new tokens, or ``_reconnect_after_forced_refresh``
    forced one) or by a forced refresh shortly before (the backoff skipped
    this one). The SDK falling into the authorization_code grant then
    proves nothing about the sign-in: only a refresh the upstream really
    refused (a captured signature) may delete it at once.

    The Step-3 connect may fail for many
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

    Steps 3 and 4 apply to the tokens the failure is about only while
    they are still the stored ones. If the user signed in again (or
    disconnected), or another refresh saved newer tokens, while this
    reconnect ran, the failure says nothing about what is stored now:
    nothing is recorded, nothing deleted.
    """
    signature = _extract_refresh_failure(oauth_auth)
    about = failure_revision(oauth_auth, storage)

    # If the SDK's 401 handler fell into authorization_code grant
    # (our ``_noop_callback`` raised), the bearer is dead AND the
    # SDK never even attempted ``refresh_token`` (per
    # ``mcp/client/auth/oauth2.py:597``). No refresh response was
    # received → ``signature`` is None. When the sign-in cannot be
    # refreshed at all (no refresh token), nothing can bring it back:
    # synthesize an ``invalid_grant`` so §5.1 deletes immediately and
    # §5.2's email pipeline notifies — instead of accumulating five
    # identical "transient" failures over half an hour while the user
    # wonders what's wrong. A sign-in WITH a refresh token first gets one
    # forced refresh (``_reconnect_after_forced_refresh``, which passes
    # ``refresh_tried``): only a refresh the upstream really refused may
    # delete it, never a short burst of 401s.
    if (
        signature is None
        and not refresh_tried
        and _exception_chain_contains(exc, SilentReconnectAuthRequired)
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

    if not await tokens_are_still_stored(
        connection_store, org_id, upstream.id, effective_user, about,
    ):
        logger.info(
            "upstream.reconnect.failure_of_replaced_tokens",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
        )
        return DisconnectReason.token_refresh_failed

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
        refused = (
            signature is not None
            and signature.error_code in TERMINAL_AUTH_ERROR_CODES
        )
        # The upstream still refuses the member's bearer (it asks for a
        # new sign-in), and no refresh failed this time (it accepted the
        # refresh token, or none was due): only signing in again brings
        # the sign-in back. A refresh that failed otherwise (a 5xx, no
        # answer) points at an outage instead.
        bearer_refused = signature is None and _exception_chain_contains(
            exc, SilentReconnectAuthRequired,
        )
        reason = (
            signature.error_code
            if refused and signature is not None
            else f"transient-threshold (failures={failure_count})"
        )
        logger.info(
            "upstream.reconnect.tokens_deleted",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            reason=reason,
            bearer_refused=bearer_refused,
            exc_info=True,
        )
        # Purge the whole per-user state, not just the token: an
        # ``invalid_client`` rejection means the DCR client_info is dead
        # too, and leaving it would re-brick the next consent. The funnel
        # also warns the member, who is now signed out, when signing in
        # again is what fixes it: a refused refresh, or a threshold
        # reached on an upstream refusing the bearer. Not when the
        # threshold is reached on network failures or upstream 5xx: during
        # a long outage it would tell every member to sign in again while
        # signing in cannot work.
        await delete_refused_sign_in(
            connection_store, org_id, upstream, effective_user,
            revision=about,
            warner=warner if refused or bearer_refused else None,
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


async def _refresh_to_completion[T](refresh: Coroutine[Any, Any, T]) -> T:
    """``refresh`` (a token refresh, with the refresh lock it holds),
    finished even if the caller is cancelled: the caller stops waiting at
    once, the refresh runs to the end (held by ``_background_tasks``),
    saves what the provider issued and only then lets go of the lock."""
    return await finish_despite_cancels(
        refresh,
        held_by=_background_tasks,
        wait_after_cancel=0,
    )


async def _silent_refresh_after_others(
    oauth_auth: OAuthClientProvider,
    upstream: UpstreamDefinition,
    *,
    org_id: str,
    user_id: str,
    refresh_lock: SignInRefreshLock,
) -> bool:
    """A reconnect's Step 1 when its refresh is due:
    ``_trigger_silent_refresh``, once any other refresh of the sign-in
    under way (the periodic refresh) has saved its tokens. The sign-in
    library then starts from those, and finds them fresh, instead of
    presenting the refresh token the other refresh just used up, which an
    upstream that rotates refresh tokens refuses (and one with reuse
    detection answers by revoking the sign-in).

    Returns ``False``, without refreshing, when the other refresh holds
    on longer than the lock's wait: a second refresh now is the race the
    lock is there to prevent."""
    assert upstream.http is not None
    async with refresh_lock.hold(org_id, upstream.id, user_id) as held:
        if not held:
            return False
        await _trigger_silent_refresh(oauth_auth, upstream.http.url)
        return True


async def _trigger_silent_refresh(
    oauth_auth: OAuthClientProvider, url: str,
) -> None:
    """Trigger a token-refresh side-effect via a lightweight probe.

    The SDK's ``OAuthClientProvider`` wraps every outgoing request: if
    the access token is within the refresh margin (or already expired
    but refresh-eligible) it transparently refreshes before forwarding.
    By the time control returns here the storage has been updated, so
    the caller can re-read the token row to see whether refresh
    actually produced a usable bearer.

    The request itself is *expected to fail*. The probe sends an MCP
    ``initialize`` it never completes a session for, so the upstream
    typically returns a non-200 or trips a transport error. That's
    fine; we only care about the auth-side-effect that has already
    happened. The bare ``except`` swallows the transport-level
    failure rather than masking a refresh problem (the refresh outcome
    is observed by the caller via ``storage.get_tokens()``).
    """
    try:
        await probe_upstream_for_auth(url, oauth_auth, timeout=10)
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


class _ReconnectRefused(Exception):
    """A stored-token reconnect ended without a session; ``reason`` says
    why. Raised inside the shared reconnect, so every caller waiting on
    that reconnect gets the same answer."""

    def __init__(self, reason: DisconnectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


async def reconnect_session_with_stored_tokens(
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore,
    client_manager: UpstreamClientManager,
    server_url: str,
    timeout: float = 15,
) -> ClientSession | DisconnectReason:
    """The user's live session, reconnected from stored tokens if there is
    none (no browser interaction). Returns the session, or why not.

    Requests that need the same user's session at the same moment share
    ONE reconnect: the token refresh, the connect and the bookkeeping after
    it run once, and every caller gets that reconnect's session or its
    reason. Two overlapping reconnects used to each refresh the token and
    each connect, and the second connect closed the session the first had
    just built (Sentry MCPOLIS-BACKEND-W). Two refreshes of one rotating
    refresh token also make the loser look revoked, and the failure
    handling below then deletes the user's sign-in.

    ``timeout`` bounds the connect of the reconnect this call starts; a
    caller that joins a running reconnect waits on that one's budget.
    """
    if upstream.http is None:
        return DisconnectReason.no_tokens

    async def reconnect(open_session: OpenUserSession) -> ClientSession:
        outcome = await _reconnect_from_stored_tokens(
            org_id=org_id,
            upstream=upstream,
            effective_user=effective_user,
            connection_store=connection_store,
            server_url=server_url,
            timeout=timeout,
            open_session=open_session,
            warner=client_manager.sign_in_warner,
            refresh_lock=client_manager.sign_in_refresh_lock,
            forced_refresh_backoff=client_manager.forced_refresh_backoff,
        )
        if isinstance(outcome, DisconnectReason):
            raise _ReconnectRefused(outcome)
        return outcome

    try:
        return await client_manager.ensure_user_session(
            upstream, effective_user, reconnect=reconnect,
        )
    except _ReconnectRefused as refused:
        return refused.reason


async def reconnect_with_stored_tokens(
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore,
    client_manager: UpstreamClientManager,
    server_url: str,
    timeout: float = 15,
) -> DisconnectReason | None:
    """``reconnect_session_with_stored_tokens`` for callers that only need
    the outcome: ``None`` once the user has a live session, else why not.
    """
    outcome = await reconnect_session_with_stored_tokens(
        org_id, upstream, effective_user, connection_store,
        client_manager, server_url, timeout,
    )
    if isinstance(outcome, DisconnectReason):
        return outcome
    return None


async def _reconnect_from_stored_tokens(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore,
    server_url: str,
    timeout: float,
    open_session: OpenUserSession,
    warner: SignInWarner | None,
    refresh_lock: SignInRefreshLock,
    forced_refresh_backoff: Backoff[ForcedRefreshKey],
) -> ClientSession | DisconnectReason:
    """One reconnect: refresh the token if due, then connect.

    Runs once per shared reconnect (see
    ``reconnect_session_with_stored_tokens``). First triggers a lightweight
    HTTP request to refresh expired tokens, then creates the real MCP
    session through ``open_session``. Its refreshes hold the sign-in's
    ``refresh_lock``, and ``forced_refresh_backoff`` says when it may
    force one (``_reconnect_after_forced_refresh``).
    """
    assert upstream.http is not None

    storage = McpTokenStorage(
        connection_store, org_id, upstream.id, effective_user,
        refresh_margin_seconds=TOKEN_REFRESH_MARGIN,
    )
    try:
        # ``get_tokens``, not ``peek_tokens``: this reconnect starts from
        # the row read here. The sign-in library loads again on its first
        # request, and whichever row it holds then is what its writes and
        # this reconnect's failure handling apply to.
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
    # A refresh that is due waits for any other refresh of the sign-in
    # under way to save its tokens first (``_silent_refresh_after_others``).
    # Shielded: a Stop or a Disconnect cancels this reconnect, and a
    # cancel landing after the provider issued new tokens but before they
    # were saved would leave a dead sign-in (many providers retire the old
    # refresh token on use). The refresh finishes, and lets go of the
    # lock; only this reconnect stops.
    if raw_token is not None and storage.refresh_due(raw_token):
        if not await _refresh_to_completion(_silent_refresh_after_others(
            oauth_auth, upstream,
            org_id=org_id, user_id=effective_user, refresh_lock=refresh_lock,
        )):
            # Nothing failed, so nothing is counted against the sign-in;
            # the next call finds the other refresh's tokens.
            logger.info(
                "upstream.reconnect.refresh_lock_busy",
                upstream_id=upstream.id,
                user=effective_user,
                org_id=org_id,
            )
            return DisconnectReason.connection_timeout
    else:
        await _refresh_to_completion(
            _trigger_silent_refresh(oauth_auth, upstream.http.url),
        )

    # Step 2: Check if we still have tokens
    refreshed_tokens = await storage.peek_tokens()
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
        session = await asyncio.wait_for(
            open_session(oauth_auth), timeout=timeout,
        )
        await _persist_post_reconnect_state(
            connection_store, oauth_auth, storage,
            org_id, upstream, effective_user,
        )
        forced_refresh_backoff.reset(_forced_refresh_key(storage))
        return session
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
    except ConnectAborted:
        # A Disconnect, a removal or a shutdown ended this reconnect: not a
        # refresh failure to count, show on the dashboard, or act on.
        raise
    except Exception as exc:
        if _refused_without_trying_refresh(exc, oauth_auth, storage):
            retried = await _reconnect_after_forced_refresh(
                exc,
                oauth_auth=oauth_auth,
                storage=storage,
                org_id=org_id,
                upstream=upstream,
                effective_user=effective_user,
                connection_store=connection_store,
                server_url=server_url,
                timeout=timeout,
                open_session=open_session,
                warner=warner,
                refresh_lock=refresh_lock,
                forced_refresh_backoff=forced_refresh_backoff,
            )
            if retried is not None:
                return retried
        return await _classify_reconnect_failure(
            exc, oauth_auth, storage, connection_store,
            org_id, upstream, effective_user, warner=warner,
            # This reconnect's own refresh saved new tokens (Step 1, or
            # the sign-in library's refresh before the connect): the
            # upstream just accepted the refresh token, so a 401 on the
            # new bearer is a short burst, not a dead sign-in.
            refresh_tried=storage.tokens_saved,
        )


def _forced_refresh_key(storage: McpTokenStorage) -> ForcedRefreshKey:
    """The sign-in ``storage`` holds, as ``forced_refresh_backoff`` keys
    it: a new sign-in of the same person starts a backoff of its own."""
    return (storage.upstream_id, storage.user_id, storage.loaded_sign_in)


def _refused_without_trying_refresh(
    exc: BaseException,
    oauth_auth: OAuthClientProvider,
    storage: McpTokenStorage,
) -> bool:
    """The upstream refused the stored bearer (the SDK fell into the
    authorization_code grant) and this reconnect never tried the refresh
    token: no refresh response was captured, no refreshed tokens saved.

    The SDK answers a 401 that way without ever trying the refresh
    token, so a short burst of 401s (the upstream's auth backend
    hiccuping, a redeploy) looks exactly like a dead sign-in."""
    return (
        _exception_chain_contains(exc, SilentReconnectAuthRequired)
        and _extract_refresh_failure(oauth_auth) is None
        and not storage.tokens_saved
    )


class _ForcedRefresh(StrEnum):
    """What ``_forced_refresh_holding_the_lock`` did."""

    # It used the refresh token: the provider tells how the upstream
    # answered.
    tried = "tried"
    # Newer tokens than the refused ones are stored: another refresh of
    # the sign-in saved them meanwhile, or the person signed in again.
    replaced = "replaced"
    # No sign-in with a refresh token is stored.
    nothing_to_try = "nothing_to_try"
    # Another refresh of the sign-in held the lock past its wait.
    busy = "busy"
    # This sign-in had a forced refresh too recently.
    backed_off = "backed_off"


async def _forced_refresh_holding_the_lock(
    forced_auth: OAuthClientProvider,
    upstream: UpstreamDefinition,
    *,
    org_id: str,
    user_id: str,
    connection_store: ConnectionStore,
    about: LoadedRevision,
    refresh_lock: SignInRefreshLock,
    forced_refresh_backoff: Backoff[ForcedRefreshKey],
) -> _ForcedRefresh:
    """The forced refresh itself. It holds the sign-in's refresh lock
    from reading the stored tokens until the refreshed ones are saved, so
    the periodic refresh can no longer use up the refresh token in
    between. That race made this refresh look refused (``invalid_grant``),
    and a refused refresh deletes the sign-in and emails the member,
    though the periodic refresh's new tokens worked."""
    assert upstream.http is not None
    async with refresh_lock.hold(org_id, upstream.id, user_id) as held:
        if not held:
            return _ForcedRefresh.busy
        stored = await connection_store.get_user_token(
            org_id, user_id, upstream.id,
        )
        if stored is None:
            return _ForcedRefresh.nothing_to_try
        if stored.revision != about:
            return _ForcedRefresh.replaced
        if not stored.refresh_token:
            return _ForcedRefresh.nothing_to_try
        if not forced_refresh_backoff.attempt(
            (upstream.id, user_id, stored.sign_in),
        ):
            return _ForcedRefresh.backed_off
        logger.info(
            "upstream.reconnect.forced_refresh",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
        )
        await _trigger_silent_refresh(forced_auth, upstream.http.url)
        return _ForcedRefresh.tried


async def _reconnect_after_forced_refresh(
    exc: BaseException,
    *,
    oauth_auth: OAuthClientProvider,
    storage: McpTokenStorage,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore,
    server_url: str,
    timeout: float,
    open_session: OpenUserSession,
    warner: SignInWarner | None,
    refresh_lock: SignInRefreshLock,
    forced_refresh_backoff: Backoff[ForcedRefreshKey],
) -> ClientSession | DisconnectReason | None:
    """Try the refresh token the upstream's 401 skipped, then connect once
    more with what it issued. ``exc``, ``oauth_auth`` and ``storage`` are
    the refused connect's.

    The refresh holds the sign-in's ``refresh_lock`` and goes ahead only
    on the ``forced_refresh_backoff`` schedule: an upstream that refuses
    even the bearer it just issued used to cost one refresh per tool call.

    Returns ``None`` when there is nothing to try (no sign-in with a
    refresh token, no app registration to refresh with): the caller then
    classifies the first failure as before. Otherwise the session, or why
    not. A refresh the upstream refused is classified with its real
    answer (an ``invalid_grant`` deletes the sign-in and warns the
    member). Anything else counts as a transient failure that keeps the
    sign-in: no answer, a forced refresh skipped (another refresh held
    the lock too long, or one was forced too recently), and a refusal
    right after a refresh the upstream accepted. When newer tokens were
    saved meanwhile, the connect is tried again with those.
    """
    assert upstream.http is not None
    forced_storage = McpTokenStorage(
        connection_store, org_id, upstream.id, effective_user,
        refresh_margin_seconds=TOKEN_REFRESH_MARGIN,
        force_refresh=True,
    )
    if await forced_storage.get_client_info() is None and not upstream.auth.client_id:
        return None
    forced_auth = await _build_oauth_provider(
        upstream, forced_storage, _noop_redirect, _noop_callback, server_url,
    )
    outcome = await _refresh_to_completion(_forced_refresh_holding_the_lock(
        forced_auth, upstream,
        org_id=org_id,
        user_id=effective_user,
        connection_store=connection_store,
        about=storage.loaded_revision,
        refresh_lock=refresh_lock,
        forced_refresh_backoff=forced_refresh_backoff,
    ))
    if outcome is _ForcedRefresh.nothing_to_try:
        return None
    if outcome in (_ForcedRefresh.busy, _ForcedRefresh.backed_off):
        logger.info(
            "upstream.reconnect.forced_refresh_skipped",
            upstream_id=upstream.id,
            user=effective_user,
            org_id=org_id,
            reason=outcome.value,
        )
        return await _classify_reconnect_failure(
            exc, oauth_auth, storage, connection_store,
            org_id, upstream, effective_user,
            warner=warner, refresh_tried=True,
        )
    if outcome is _ForcedRefresh.tried:
        if _extract_refresh_failure(forced_auth) is not None:
            # The upstream answered the refresh: act on its real answer.
            return await _classify_reconnect_failure(
                exc, forced_auth, forced_storage, connection_store,
                org_id, upstream, effective_user,
                warner=warner, refresh_tried=True,
            )
        if not forced_storage.tokens_saved:
            # The refresh never got an answer (network, timeout): transient.
            return await _classify_reconnect_failure(
                exc, forced_auth, forced_storage, connection_store,
                org_id, upstream, effective_user,
                warner=warner, refresh_tried=True,
            )
    retry_storage = McpTokenStorage(
        connection_store, org_id, upstream.id, effective_user,
        refresh_margin_seconds=TOKEN_REFRESH_MARGIN,
    )
    retry_auth = await _build_oauth_provider(
        upstream, retry_storage, _noop_redirect, _noop_callback, server_url,
    )
    try:
        session = await asyncio.wait_for(
            open_session(retry_auth), timeout=timeout,
        )
    except TimeoutError:
        await connection_store.set_connection_error(
            org_id, upstream.id, DisconnectReason.connection_timeout
        )
        return DisconnectReason.connection_timeout
    except ConnectAborted:
        raise
    except Exception as retry_exc:
        # Refused again right after a refresh the upstream accepted:
        # the refresh token works, so this is not a dead sign-in.
        return await _classify_reconnect_failure(
            retry_exc, retry_auth, retry_storage, connection_store,
            org_id, upstream, effective_user,
            warner=warner, refresh_tried=True,
        )
    await _persist_post_reconnect_state(
        connection_store, retry_auth, retry_storage,
        org_id, upstream, effective_user,
    )
    forced_refresh_backoff.reset(_forced_refresh_key(retry_storage))
    return session


# ``SessionUnavailable.reason`` when an admin stopped the upstream: the
# refusal is expected, so callers log it quietly.
UPSTREAM_STOPPED = "upstream_stopped"


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
      from stored tokens (transparent token refresh), joining a reconnect
      already running for that user. ``effective_user``
      is the calling user for ``per_user_oauth`` and the slot owner for
      ``admin_oauth``; the caller resolves it. Ignored for
      ``service_account``.

    Raises :class:`SessionUnavailable` when no session can be obtained
    without interactive sign-in.
    """
    # Local import mirrors the existing deferral in this module
    # (reconnect_all_oauth_upstreams) to avoid a policy↔service cycle.
    from mcpolis.domain.model.policy import AuthMode

    # Both branches hand back the session the connect produced (or the
    # live one it found). They never connect and then look the session up
    # again: in between, another request could replace it, and the lookup
    # then finds nothing (the KeyError of Sentry MCPOLIS-BACKEND-W) or,
    # worse, falls through to the shared discovery session and runs a
    # per-user call without the user's sign-in.
    if upstream.auth.mode == AuthMode.service_account:
        try:
            return await client_manager.ensure_shared_connected(upstream)
        except UpstreamStopped as exc:
            raise SessionUnavailable(UPSTREAM_STOPPED) from exc
        except Exception as exc:
            raise SessionUnavailable("connect_failed") from exc

    if connection_store is None:
        raise SessionUnavailable("oauth_not_configured")

    try:
        outcome = await reconnect_session_with_stored_tokens(
            org_id=org_id,
            upstream=upstream,
            effective_user=effective_user,
            connection_store=connection_store,
            client_manager=client_manager,
            server_url=server_url,
            timeout=timeout,
        )
    except ConnectAborted as exc:
        # An admin stopped the upstream (refused until their Start, even
        # though the user's saved sign-in is kept), or a Stop, a shutdown
        # or a Disconnect cancelled the connect this call waited on.
        if isinstance(exc, UpstreamStopped) or client_manager.is_stopped(
            upstream.id,
        ):
            raise SessionUnavailable(UPSTREAM_STOPPED) from exc
        raise SessionUnavailable("connect_aborted") from exc
    except Exception as exc:
        # Anything the reconnect did not classify itself. Reaching the
        # caller raw would send its text to the MCP client, which may carry
        # upstream URLs or internal addresses; log it (ERROR, so it alerts)
        # and hand back a clean "no session".
        logger.exception(
            "upstream.acquire.reconnect_failed",
            org_id=org_id,
            upstream_id=upstream.id,
            user=effective_user,
        )
        raise SessionUnavailable("connect_failed") from exc
    if isinstance(outcome, DisconnectReason):
        raise SessionUnavailable(outcome)
    return outcome


async def heal_stalled_session(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    client_manager: UpstreamClientManager,
    stalled_session: ClientSession,
) -> None:
    """Drop the session whose transport just stalled, so the next
    acquisition runs on a fresh transport.

    ``stalled_session`` is the session the caller saw stall. Only that
    session is dropped: by the time this runs, another request that hit
    the same stall may already have replaced it, and dropping "whatever is
    there now" would kill that fresh session under whoever moved onto it.
    For the same reason a heal never stops a connect that is running: that
    connect is the replacement.

    - ``service_account``: drop the shared session and reconnect fresh
      (the sandbox service fresh-creates rather than reattaching to the
      same flaky sandbox).
    - OAuth modes: evict the cached per-user session.
      ``acquire_upstream_session`` short-circuits to the cache on
      membership alone — no liveness check — so without eviction a
      stall retry (and every later call) is handed the same dead
      session until the idle sweep removes it. Prod incident
      2026-06-12 (Sentry MCPOLIS-BACKEND-R/-S): a per-user mixpanel
      session died during an idle gap and the user's calls failed with
      ``ClosedResourceError`` for the rest of the sweep window.
      After eviction the next acquisition falls through to
      ``reconnect_with_stored_tokens`` (no browser interaction).
    """
    # Local import mirrors the existing deferral in this module to
    # avoid a policy↔service cycle.
    from mcpolis.domain.model.policy import AuthMode

    if upstream.auth.mode == AuthMode.service_account:
        await client_manager.reconnect_shared_fresh(
            upstream, stale=stalled_session,
        )
        return
    evicted = await client_manager.evict_user_session_if_current(
        upstream.id, effective_user, stalled_session,
    )
    logger.info(
        "upstream.session.stall_evicted",
        org_id=org_id,
        upstream_id=upstream.id,
        user=effective_user,
        auth_mode=upstream.auth.mode.value,
        evicted=evicted,
    )


async def settle_oauth_state_after_stall(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore | None,
    client_manager: UpstreamClientManager,
    server_url: str,
) -> None:
    """Run the dead-token reconnect probe a non-retry-safe stall can't.

    A gateway tool call runs on a CACHED live OAuth session whose SDK
    provider uses ``_noop_callback``. When the upstream revokes the bearer
    the SDK's mid-call silent refresh fails and the transport goes silent,
    so the dispatch sees a transport STALL (not the buried
    ``SilentReconnectAuthRequired``). ``heal_stalled_session`` evicts the
    dead session, but a NON-retry-safe verb (``max_attempts == 1``) never
    reconnects — so §5.1's classify-and-delete (which only runs inside
    ``reconnect_with_stored_tokens``) never fires, the stored token row
    survives, and ``resolve_upstream_readiness`` keeps the dashboard
    showing "Ready" though every call now fails with re-auth.

    A retry-SAFE verb closes this gap for free: its retry's
    ``_resolve_session`` reconnects (and classifies dead tokens) on the
    next attempt. This helper gives the non-retry-safe path the SAME
    reconnect probe — re-establishing the session on a transient stall, or
    classifying-and-deleting on genuinely revoked tokens — without
    re-running the (non-idempotent) op.

    OAuth-only: ``service_account`` stalls already fresh-reconnect inside
    ``heal_stalled_session``.
    """
    # Local import mirrors the existing deferral in this module to avoid a
    # policy↔service cycle.
    from mcpolis.domain.model.policy import AuthMode

    if connection_store is None:
        return
    if upstream.auth.mode == AuthMode.service_account:
        return
    try:
        await reconnect_with_stored_tokens(
            org_id=org_id,
            upstream=upstream,
            effective_user=effective_user,
            connection_store=connection_store,
            client_manager=client_manager,
            server_url=server_url,
            # The dead-token case the probe targets fails FAST (the SDK
            # falls into authorization_code grant immediately, no network
            # wait), so the dashboard is settled before the failed call
            # returns. A tighter-than-default timeout caps the rare
            # transient-unreachable case so a non-retry-safe caller isn't
            # made to wait the full default reconnect budget for an error
            # whose outcome is already decided.
            timeout=PROBE_RECONNECT_TIMEOUT,
        )
    except ConnectAborted:
        # A Stop (or a Disconnect) ended the probe's reconnect: expected,
        # not a fault to alert on.
        logger.info(
            "upstream.dispatch.oauth_reconnect_probe_aborted",
            org_id=org_id,
            upstream_id=upstream.id,
            user=effective_user,
        )
    except Exception:
        logger.exception(
            "upstream.dispatch.oauth_reconnect_probe_failed",
            org_id=org_id,
            upstream_id=upstream.id,
            user=effective_user,
        )


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
    persisting a half-empty catalogue; here we drop the stalled session
    (``heal_stalled_session``: fresh shared reconnect for
    service_account, per-user eviction for OAuth), and retry. The
    operator gets a complete tool/resource/prompt list instead of a
    partial one or a 30s hang.

    Non-stall failures (OAuth not configured, genuine server errors)
    propagate immediately — only a transport stall is retried, and only
    ``max_attempts`` times.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        session = await acquire_upstream_session(
            org_id=org_id,
            upstream=upstream,
            effective_user=effective_user,
            connection_store=connection_store,
            client_manager=client_manager,
            server_url=server_url,
        )
        try:
            # Discover on the session just acquired. Looking one up again
            # would race anything that replaces or drops it in between.
            return await tool_registry.refresh_upstream(
                upstream.id, session=session,
            )
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
            # Force a fresh transport for the next attempt. Dropping
            # the stalled session is required for OAuth too:
            # ``acquire_upstream_session`` above short-circuits to the
            # cached per-user session, dead or not, so without eviction
            # the retry would refresh over the same closed transport.
            try:
                await heal_stalled_session(
                    org_id=org_id,
                    upstream=upstream,
                    effective_user=effective_user,
                    client_manager=client_manager,
                    stalled_session=session,
                )
            except UpstreamStopped as stopped:
                # Stopped while this refresh ran: nothing to heal, and a
                # refusal the admin asked for, not a failure.
                raise SessionUnavailable(UPSTREAM_STOPPED) from stopped
    # Loop always returns or raises; this satisfies the type checker.
    assert last_exc is not None
    raise last_exc


def recovery_effective_user(
    client_manager: UpstreamClientManager,
    upstream: UpstreamDefinition,
) -> str:
    """The ``effective_user`` an admin-MCP refresh should reattach under
    when routed through ``acquire_and_refresh_with_recovery`` (R6).

    ``acquire_and_refresh_with_recovery`` is identity-coupled (one
    ``effective_user``) whereas admin discovery is identity-AGNOSTIC — it
    reuses any user's live session. So:

    - ``service_account`` → the shared session (``""``).
    - OAuth → the user who actually OWNS the live discovery session,
      which may NOT be the calling admin (``_ensure_oauth_session`` skips
      establishing one when any user already has a session). A stall must
      heal under that user to reconnect from the RIGHT stored tokens;
      passing the caller would evict/reconnect the wrong identity.

    Falls back to ``""`` when no per-user session exists — the recovery
    then surfaces ``SessionUnavailable``, the same outcome a plain
    refresh against a missing session would produce.
    """
    from mcpolis.domain.model.policy import AuthMode

    if upstream.auth.mode == AuthMode.service_account:
        return ""
    return client_manager.first_user_with_session(upstream.id) or ""


async def refresh_all_with_recovery(
    *,
    org_id: str,
    connection_store: ConnectionStore | None,
    client_manager: UpstreamClientManager,
    tool_registry: ToolRegistry,
    server_url: str,
) -> None:
    """``refresh_all`` with per-upstream transport-stall recovery (R6).

    Like ``ToolRegistry.refresh_all`` it refreshes every currently-connected
    upstream and tolerates per-upstream failures so one bad upstream doesn't
    abort the sweep — but routes each refresh through
    ``acquire_and_refresh_with_recovery`` so an E2B post-reattach stall heals
    (fresh reconnect for service_account, per-user eviction for OAuth) and
    retries instead of persisting a half-empty catalogue.

    Runs the per-upstream refreshes CONCURRENTLY (review item 5), mirroring
    ``reconnect_all_oauth_upstreams``: each upstream has its own session /
    sandbox / per-upstream connect lock, so they don't contend, and the
    admin-MCP caller blocks for the SLOWEST single upstream rather than the
    SUM — bounding the worst case from N×2×(establish+timeout) to one. The
    block is still intended: the ``refresh_upstream_tools`` tool's contract
    is to report the discovered tool count synchronously (unlike the
    dashboard's non-blocking refresh, c54c0b3), so it cannot be backgrounded.
    A mid-sweep client cancellation leaves the already-completed upstreams
    refreshed (each ``refresh_upstream`` writes through the catalog as it
    finishes) and the rest stale — degraded, not corrupt; the admin re-runs.
    """
    async def _one(upstream_id: str) -> None:
        upstream = client_manager.get_upstream(upstream_id)
        if upstream is None:
            return
        try:
            await acquire_and_refresh_with_recovery(
                org_id=org_id,
                upstream=upstream,
                effective_user=recovery_effective_user(
                    client_manager, upstream,
                ),
                connection_store=connection_store,
                client_manager=client_manager,
                tool_registry=tool_registry,
                server_url=server_url,
            )
        except Exception:
            logger.exception(
                "tool.registry.refresh.failed",
                org_id=org_id,
                upstream_id=upstream_id,
            )

    tasks = [
        _one(upstream_id)
        for upstream_id in list(client_manager.connected_upstream_ids)
    ]
    if tasks:
        # return_exceptions=True belt-and-braces — ``_one`` already swallows
        # its own failures, but this guarantees one surprising raise can't
        # cancel the sibling refreshes mid-flight.
        await asyncio.gather(*tasks, return_exceptions=True)


# Minimum time the "Fetching info" pill stays on screen after a connect.
# Without a floor, a warm-cache refresh can finish in <2s and the pill
# briefly flashes — which reads as a bug rather than progress.
_MIN_REFRESHING_DISPLAY_SECONDS = 4.0


async def _refresh_upstream_in_background(
    tool_registry: ToolRegistry,
    client_manager: UpstreamClientManager,
    upstream_id: str,
    on_refreshed: Callable[[], None] | None = None,
    on_discovery_done: Callable[[str | None], None] | None = None,
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

    ``on_discovery_done`` hears the outcome as soon as discovery ends,
    before that display floor: the error text, or ``None`` on success.
    The text is shown to admins, so it carries no password Variable
    (``client_manager.hide_secrets_in_error``).
    ``on_refreshed`` fires after the floor, success or not.
    """
    started_at = tool_registry.refreshing_started_at(upstream_id)
    try:
        await tool_registry.refresh_upstream(upstream_id)
    except asyncio.CancelledError:
        _report_discovery(
            on_discovery_done, upstream_id, "tool discovery was cancelled",
        )
        raise
    except Exception as exc:
        logger.exception(
            "upstream.refresh.background_failed", upstream_id=upstream_id,
        )
        _report_discovery(
            on_discovery_done, upstream_id,
            client_manager.hide_secrets_in_error(
                upstream_id, str(exc) or exc.__class__.__name__,
            ),
        )
    else:
        _report_discovery(on_discovery_done, upstream_id, None)
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


def _report_discovery(
    on_discovery_done: Callable[[str | None], None] | None,
    upstream_id: str,
    error: str | None,
) -> None:
    if on_discovery_done is None:
        return
    try:
        on_discovery_done(error)
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
    on_discovery_done: Callable[[str | None], None] | None = None,
    sign_in_check: Callable[[], Awaitable[str | None]] | None = None,
) -> OAuthConnectResult:
    """Initiate OAuth connection; refresh the tool catalog in the background.

    ``mark_refreshing`` is flipped synchronously up front, so the
    dashboard's "Fetching info" pill is observable for the entire
    duration of the slow path — both the MCP session establishment
    inside ``initiate_oauth_connection`` and the catalog refresh that
    follows. The flag is cleared on early-exit branches (pending OAuth
    redirect / error) and otherwise lives until the background catalog
    task finishes. ``on_tools_refreshed`` fires once that task is done;
    ``on_discovery_done`` hears the discovery outcome as soon as it is
    known (see ``_refresh_upstream_in_background``). ``sign_in_check``:
    see ``initiate_oauth_connection``.
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
            sign_in_check=sign_in_check,
        )
    except BaseException:
        tool_registry.unmark_refreshing(upstream.id)
        raise
    if result.connected:
        # Background task takes ownership of the flag; unmarks on done.
        _background_tasks.spawn(
            _refresh_upstream_in_background(
                tool_registry, client_manager, upstream.id, on_tools_refreshed,
                on_discovery_done,
            )
        )
    else:
        # Pending (auth URL returned) or error: clear so the pill
        # doesn't stick. The follow-up second connect call will
        # mark again.
        tool_registry.unmark_refreshing(upstream.id)
    return result


# Ordering key for sign-in rows with no time at all (saved before
# ``updated_at`` existed): they sort before any timed row, so a fresh
# sign-in wins, and among themselves in admin order.
_LEGACY_SIGN_IN_TS = datetime.min.replace(tzinfo=UTC)


async def slot_owner_of(
    connection_store: ConnectionStore,
    org_id: str,
    upstream_id: str,
    *,
    admin_emails: list[str],
) -> str | None:
    """The admin whose saved sign-in the admin tab shows for an OAuth
    upstream ("Ready, by alice@"), or ``None`` when no admin holds one.

    When several admins hold a sign-in (possible for ``per_user_oauth``),
    the one who signed in last wins: ``OAuthToken.sign_in_time``, which a
    token refresh keeps, so the shown owner is stable (see Risk 1 of
    internal/plans/upstream-readiness-uniform-oauth.md). It used to be
    the last SAVED row: every refresh is a save, so each refresh of
    another admin's token moved the slot to them, and an ``admin_oauth``
    upstream's tool calls with it. A row saved before sign-in times
    existed counts its last save, so the deploy that added them left the
    slot where it was. Ties go to the first admin in ``admin_emails``.
    Start after a Stop and Remove sign-in act on this same admin.
    """
    best: tuple[str, datetime] | None = None
    for email in admin_emails:
        token = await connection_store.get_user_token(
            org_id, email, upstream_id,
        )
        if token is None:
            continue
        signed_in = token.sign_in_time
        ts = signed_in if signed_in is not None else _LEGACY_SIGN_IN_TS
        if best is None or ts > best[1]:
            best = (email, ts)
    return best[0] if best is not None else None


async def stop_keeping_sign_ins(
    *,
    org_id: str,
    upstream_id: str,
    client_manager: UpstreamClientManager,
    connection_store: ConnectionStore | None,
) -> None:
    """An admin's Stop (the dashboard's Stop / Disconnect, the Admin MCP's
    ``disconnect_upstream``).

    Closes every live session to the upstream, the shared one and each
    user's own, and keeps it stopped across restarts. Deletes no saved
    sign-in: after Start, nobody signs in again.

    Saves the stop first, so a restart while the sessions close still
    finds it stopped, and marks this app stopped right after with no await
    in between. Runs one at a time with Start (``reopen_stopped_upstream``)
    so the saved state and the app's state always agree.
    """
    async with client_manager.stop_start_lock(upstream_id):
        if connection_store is not None:
            # Explicit False rather than removing the marker.
            await connection_store.set_disabled(org_id, upstream_id)
        try:
            await client_manager.disconnect_upstream(upstream_id)
        except Exception:
            # Storage follows the app: a Stop that failed after the app
            # stopped the upstream (its sandbox clean-up erred) stays
            # saved stopped; one that failed before leaves it running,
            # and so saved as started.
            if (
                connection_store is not None
                and not client_manager.is_stopped(upstream_id)
            ):
                await connection_store.set_enabled(org_id, upstream_id)
            raise
        if connection_store is not None:
            await connection_store.clear_connection_error(org_id, upstream_id)


async def sign_out_of_upstream(
    *,
    org_id: str,
    upstream_id: str,
    user_id: str,
    connection_store: ConnectionStore,
    client_manager: UpstreamClientManager,
) -> None:
    """Sign one user out of an upstream: a member's own sign-out on My
    Tools, or an admin's Sign out of the admin sign-in the admin tab
    shows. Deletes that user's saved sign-in, then closes their live
    session and stops a connect still running for them. Every other
    user's sign-in and session stay.

    The sign-in goes first, so a call still resolving the user cannot
    reconnect from it once the session is gone.
    """
    await connection_store.delete_user_token(org_id, user_id, upstream_id)
    await client_manager.disconnect_user_session(upstream_id, user_id)


async def reopen_stopped_upstream(
    *,
    org_id: str,
    upstream_id: str,
    client_manager: UpstreamClientManager,
    connection_store: ConnectionStore | None,
) -> None:
    """Undo ``stop_keeping_sign_ins`` for an OAuth upstream (an admin's Start):
    every user's session may open again, from their saved sign-in. Runs
    one at a time with Stop (``stop_keeping_sign_ins``) and removal.

    Raises ``UpstreamStopped`` when the upstream was removed meanwhile
    (the caller read its definition before the removal): saving it
    started would leave a stale row for a later add under the same id."""
    async with client_manager.stop_start_lock(upstream_id):
        if client_manager.is_removed(upstream_id):
            raise UpstreamStopped(f"upstream {upstream_id!r} was removed")
        if connection_store is not None:
            await connection_store.set_enabled(org_id, upstream_id)
        client_manager.transition_out_of_disabled(upstream_id)


async def start_shared_in_background(
    *,
    org_id: str,
    upstream_id: str,
    client_manager: UpstreamClientManager,
    connection_store: ConnectionStore | None,
    connect: Callable[[], Awaitable[object]],
) -> asyncio.Task[None]:
    """The dashboard's Start of an upstream without sign-in: save it as
    started and launch its connect, which runs on in the background (the
    dashboard shows Starting… until it ends; a Stop aborts it).

    The caller holds the Stop/Start lock (``client_manager.stop_start_lock``)
    from its own checks through this, so a Stop, another Start or a
    removal lands before or after the whole Start: never between the save
    and the launch, which would leave the server running while storage
    says stopped, and never between the Start's checks and the launch.
    """
    if not client_manager.stop_start_lock(upstream_id).locked():
        raise RuntimeError("start_shared_in_background needs the Stop/Start lock")

    async def run() -> None:
        await connect()

    if connection_store is not None:
        await connection_store.set_enabled(org_id, upstream_id)
    # Held by the manager (``register_background_connect_task``) until it
    # ends, also after the connect lands.
    task = asyncio.create_task(run())
    client_manager.register_background_connect_task(upstream_id, task)
    return task


async def start_from_saved_sign_in(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    owner: str,
    connection_store: ConnectionStore,
    client_manager: UpstreamClientManager,
    tool_registry: ToolRegistry,
    server_url: str,
    on_tools_refreshed: Callable[[], None] | None = None,
    on_discovery_done: Callable[[str | None], None] | None = None,
) -> OAuthConnectResult:
    """Start a stopped OAuth upstream whose admin sign-in Stop kept.

    Reopens it and reconnects ``owner`` (the admin holding that sign-in,
    whoever clicks Start) from the stored tokens. Never opens a sign-in
    page: an admin clicking Start must not be walked through signing in
    as another admin. When the saved sign-in no longer works the error
    comes back; a revoked one was deleted on the way, so the dashboard
    then offers Authenticate.
    """
    await reopen_stopped_upstream(
        org_id=org_id,
        upstream_id=upstream.id,
        client_manager=client_manager,
        connection_store=connection_store,
    )
    tool_registry.mark_refreshing(upstream.id)
    try:
        reason = await reconnect_with_stored_tokens(
            org_id, upstream, owner,
            connection_store, client_manager, server_url,
        )
    except ConnectAborted:
        # A Stop landed during the reconnect: it owns the outcome.
        tool_registry.unmark_refreshing(upstream.id)
        logger.info(
            "upstream.start.saved_sign_in.aborted",
            org_id=org_id,
            upstream_id=upstream.id,
        )
        return OAuthConnectResult(aborted=True)
    except BaseException:
        tool_registry.unmark_refreshing(upstream.id)
        raise
    if reason is not None:
        tool_registry.unmark_refreshing(upstream.id)
        return OAuthConnectResult(
            error=f"The saved sign-in of {owner} did not reconnect: {reason}",
        )
    # Background task takes ownership of the flag; unmarks on done.
    _background_tasks.spawn(
        _refresh_upstream_in_background(
            tool_registry, client_manager, upstream.id, on_tools_refreshed,
            on_discovery_done,
        )
    )
    return OAuthConnectResult(connected=True)


def refresh_tools_in_background(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    effective_user: str,
    connection_store: ConnectionStore | None,
    client_manager: UpstreamClientManager,
    tool_registry: ToolRegistry,
    server_url: str,
    on_success: Callable[[], Awaitable[None]] | None = None,
    on_error: Callable[[str], Awaitable[None]] | None = None,
    on_discovery_done: Callable[[str | None], None] | None = None,
) -> "asyncio.Task[None]":
    """Run a tool-catalog refresh off the request path; return at once.

    The dashboard refresh endpoint used to ``await`` the full
    acquire+refresh synchronously. After an E2B auto-pause that path can
    stall ~15s and recover by reconnecting on a fresh session, blowing the
    request's budget — so the operator saw a ``TimeoutError`` for a
    refresh that actually completed seconds later in the background (prod
    incident 2026-06-18). This decouples the two:

    - ``mark_refreshing`` flips synchronously (before this returns), so the
      very next ``GET /api/admin/upstreams`` shows the "Fetching info" pill;
    - the slow acquire+refresh+recovery runs in a task;
    - completion is surfaced via the pill clearing + ``tools/list_changed``
      + the per-upstream connection-error state — NOT via the HTTP
      response, which returns immediately.

    ``on_success`` / ``on_error`` (async) let the caller record the outcome
    (clear/set connection error, audit log, policy broadcast); they run
    AFTER the refreshing flag clears. ``on_discovery_done`` hears the
    outcome as soon as discovery ends, before that display floor: the
    error text, or ``None`` on success (the Admin MCP answers with it).
    Returns the spawned task so callers (and tests) can await it; the
    endpoint ignores it, which is safe because the module holds the task
    until it ends. Mirrors :func:`connect_and_refresh_tools`'
    mark-then-background structure.
    """
    tool_registry.mark_refreshing(upstream.id)

    async def _discover() -> str | None:
        """Discover the tools; the error text, or ``None`` on success."""
        try:
            await acquire_and_refresh_with_recovery(
                org_id=org_id,
                upstream=upstream,
                effective_user=effective_user,
                connection_store=connection_store,
                client_manager=client_manager,
                tool_registry=tool_registry,
                server_url=server_url,
            )
        except SessionUnavailable as exc:
            return f"could not reattach session: {exc.reason}"
        except Exception as exc:  # noqa: BLE001
            return client_manager.hide_secrets_in_error(
                upstream.id, str(exc) or exc.__class__.__name__,
            )
        return None

    async def _bg() -> None:
        started_at = tool_registry.refreshing_started_at(upstream.id)
        try:
            error_msg = await _discover()
            _report_discovery(on_discovery_done, upstream.id, error_msg)
        finally:
            # Keep the pill on screen for the floor duration even if the
            # refresh was fast or failed fast — same as the connect path.
            if started_at is not None:
                remaining = (
                    _MIN_REFRESHING_DISPLAY_SECONDS
                    - (time.monotonic() - started_at)
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
            tool_registry.unmark_refreshing(upstream.id)
        if error_msg is not None:
            logger.warning(
                "upstream.refresh.background_failed",
                upstream_id=upstream.id, org_id=org_id, error=error_msg,
            )
            if on_error is not None:
                await on_error(error_msg)
        elif on_success is not None:
            await on_success()

    return _background_tasks.spawn(_bg())


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
    "reconnect_session_with_stored_tokens",
    "connect_and_refresh_tools",
    "reconnect_all_oauth_upstreams",
    "refresh_token_for_user",
]
