"""MCP gateway OAuth Authorization Server Provider — issues bearer tokens to MCP clients, delegating identity to Google.

The dashboard's browser-login flow (cookie-issuing) lives in a sibling
adapter ``adapters/auth/google_oauth_provider.py`` and uses a different
port; this module is exclusively for the MCP gateway surface.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlencode, urlparse

import httpx
import structlog
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from mcpolis.domain.model.email_address import email_key, same_email
from mcpolis.domain.model.events import Event
from mcpolis.domain.model.service_token import (
    is_reserved_scope,
    strip_reserved_scopes,
)
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    OAuthStateRepository,
    StoredAccessToken,
    StoredClient,
    StoredClientApproval,
    StoredRefreshToken,
    client_approval_key,
)
from mcpolis.domain.services.background_tasks import BackgroundTaskSet

if TYPE_CHECKING:
    from mcpolis.domain.services.org_runtime import OrgRuntimeManager
    from mcpolis.domain.services.org_service import Invitation, OrgService

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Consent tokens tie a human's approve/deny decision back to a pending
# authorization. Short-lived and single-use. Kept short because the token
# rides in the consent-page URL: a tight window limits how long a leaked
# access log line could be replayed before it expires.
CONSENT_TTL = 180  # 3 minutes

# Loopback hosts for which cleartext ``http://`` redirect URIs are
# allowed (native clients listening on localhost). IPv6 host comes back
# from ``AnyUrl`` bracketed (``[::1]``) — bracket-stripped before match.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_registrable_redirect_uri(uri: AnyUrl) -> bool:
    """Whether an open-registration client may register this redirect.

    Allowed: ``https://`` to any host (remote web connectors like
    Claude.ai — a remote ``https`` host can't be blocked here without
    breaking them, so the consent step gates a hostile one); loopback
    ``http://`` (Claude Code / Cursor local listeners); and private-use
    URI schemes with a host (native apps, RFC 8252, e.g. ``cursor://``).

    Rejected:
    - cleartext ``http://`` to a non-loopback host — ships a code to an
      arbitrary server in the clear;
    - any redirect carrying *userinfo* (``https://good.example@evil.com``)
      — the authority then reads as one host but the browser delivers to
      another, which would let the consent page display a host the code
      never goes to (see ``_redirect_identity``);
    - schemes with no host (``javascript:``, ``data:``, ``file:``) — not
      a real redirect destination.
    """
    parsed = urlparse(str(uri))
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if parsed.username is not None:
        return False
    if scheme == "http":
        return host in _LOOPBACK_HOSTS
    return True


def _redirect_identity(redirect_uri: str) -> str:
    """The security identity of a redirect URI: ``scheme://host``.

    Deliberately drops userinfo, port and path. This single string is
    BOTH what the consent page shows the user AND what the remembered
    approval is keyed on, so a skipped-consent flow can only deliver a
    code to the exact ``scheme://host`` the user saw and approved.

    Port is dropped so native clients on an ephemeral loopback port
    (``http://127.0.0.1:<random>``) are approved once rather than
    re-prompted every launch; the host alone is the exfiltration
    boundary (a loopback host is the user's own machine). Path is
    dropped because a code delivered anywhere on a host reaches the
    same server.
    """
    parsed = urlparse(redirect_uri)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    return f"{scheme}://{host}" if host else scheme

# Token lifetimes
ACCESS_TOKEN_TTL = 3600  # 1 hour
# Refresh tokens expire after 30 days and are rotated on every use (the
# old token is removed and a new one is minted during
# ``exchange_refresh_token``). Set to ``None`` to disable expiry entirely
# (not recommended outside local development).
REFRESH_TOKEN_TTL: int | None = 30 * 86400  # 30 days

# Open client registration (``/register``) and ``/authorize`` answer
# anyone, so what they may leave behind is capped: per request, per
# source, and in total. A real MCP client sends well under 1 KB.
#
# A sign-in request (``/register``, ``/authorize``, ``/token``,
# ``/revoke``): its body, and its query string, are refused with HTTP 413
# above this size, before they are parsed.
MAX_SIGN_IN_REQUEST_BYTES = 16 * 1024
# Any one text value kept from an anonymous request: a registration's
# name, URI, scope, contact... (and the JSON of a ``jwks`` object), or an
# ``/authorize`` request's state, PKCE challenge, resource or scope.
MAX_SIGN_IN_TEXT = 2048
# Any one list in a registration (redirect URIs, contacts, grant types...),
# and the scopes of a registration, a sign-in or a token (a scope text
# is a list of names separated by spaces). The MCP SDK compares every
# scope a sign-in asks for with every registered one, so these two
# bounds also bound that work.
MAX_REGISTRATION_LIST = 10

# A registration that never received a token is forgotten after this
# long. A sign-in takes minutes: the client registers, then the person
# signs in with Google and approves the client. A day still covers
# someone who starts it and finishes later the same day, while abandoned
# and junk registrations stop piling up. A client that received a token
# is kept for good (see ``StoredClient``).
UNUSED_REGISTRATION_TTL = 24 * 3600
# How often expired tokens and abandoned registrations are deleted.
CLEANUP_INTERVAL = 15 * 60

# Each kind of sign-in state below is capped twice: per source, and in
# total. The source of an unused registration or a pending sign-in is
# the address its request came from, as the rate limiter counts it but
# an IPv6 address per /48 (``sign_in_source`` in the rate-limit
# middleware: one /48 holds 65,536 /64s); the source of a code or a
# consent page is the member who signed in. A new item from a source
# holding ``MAX_PER_SOURCE`` makes that source's oldest go. When the total
# is reached, the oldest item of the source holding the most goes, the
# new item's own source first of those holding as many. So a flood, from
# one source or from many, pushes out its own items first: a real
# person's sign-in (one item from its source) is pushed out only once
# every source holds a single item, which takes as many sources as the
# total cap.
#
# 100: at the per-address sign-in rate limit (30 requests a minute, about
# 4 per sign-in), one address starts at most 75 sign-ins within a pending
# sign-in's 10 minutes, so a busy office behind one address never
# reaches it; and a registration still waiting for its sign-in is pushed
# out by its own address only after 100 newer ones from it (over 3
# minutes of requests at that limit).
MAX_PER_SOURCE = 100

# At most this many registrations that never received a token, counting
# every source together. Memory and storage stay bounded however many
# addresses register: at the largest a request may be, one takes about
# 18 KB in memory (measured), so 2,000 take about 36 MB. A registration
# needs its place only while its person signs in (minutes), far below
# 2,000 at once for this product.
MAX_UNUSED_REGISTRATIONS = 2_000

# A pending sign-in (an ``/authorize`` request waiting for the person at
# Google's sign-in page) is forgotten after this long. Picking an
# account, a password and a second factor take a few minutes; ten leave
# room to spare. A late answer from Google gets "Invalid or expired
# OAuth state", and the person starts again from their client.
PENDING_SIGN_IN_TTL = 10 * 60
# At most this many pending sign-ins, counting every source together.
# Every kept value is capped: the state, PKCE challenge and resource at
# ``MAX_SIGN_IN_TEXT`` each, the scopes at ``MAX_REGISTRATION_LIST`` names
# and ``MAX_SIGN_IN_TEXT`` characters in all, the redirect URI by the
# registration. One then takes about 10 KB at most (measured), so 2,000
# take about 21 MB; each also keeps its client's registration in memory,
# even one already pushed out (at most another 36 MB). Real ones need
# their place for minutes.
MAX_PENDING_SIGN_INS = 2_000

# An authorization code must be exchanged within this long; a client
# exchanges it at once, in the same browser round trip.
AUTH_CODE_TTL = 300  # 5 minutes
# At most this many codes not yet exchanged, and this many sign-ins
# waiting at the consent page (``CONSENT_TTL``), counting every member
# together. Only a member who just signed in with Google creates either,
# but one member's Google session can be replayed from many addresses,
# so they are counted per member. One holds the values of its pending
# sign-in, so about 10 KB at most: 2,000 take about 21 MB. Real ones need
# their place for seconds (a code) or until a click (a consent page).
MAX_UNEXCHANGED_CODES = 2_000
MAX_PENDING_CONSENTS = 2_000

# A write to storage that failed is tried again after this long, then
# after twice as long each time, up to ``WRITE_RETRY_MAX_DELAY``, until it
# lands. A revoke or a token rotation must reach storage even when no
# other sign-in comes along to carry it: a crash would otherwise bring
# the tokens back.
WRITE_RETRY_FIRST_DELAY = 1.0
WRITE_RETRY_MAX_DELAY = 60.0

# The client ``mint_test_token`` signs people in with (test mode only).
TEST_CLIENT_ID = "test-mcp-client"


def _size_problem(value: object) -> str | None:
    """Why one value of an anonymous request is too large to keep, or
    ``None``."""
    if isinstance(value, str):
        if len(value) > MAX_SIGN_IN_TEXT:
            return f"longer than {MAX_SIGN_IN_TEXT} characters"
        return None
    if isinstance(value, list):
        items: list[object] = value  # pyright: ignore[reportUnknownVariableType]
        if len(items) > MAX_REGISTRATION_LIST:
            return f"more than {MAX_REGISTRATION_LIST} entries"
        for item in items:
            problem = _size_problem(item)
            if problem is not None:
                return problem
        return None
    if isinstance(value, dict) and len(json.dumps(value)) > MAX_SIGN_IN_TEXT:
        return f"longer than {MAX_SIGN_IN_TEXT} characters as JSON"
    return None


def _scope_problem(scope: str | None) -> str | None:
    """Why a scope text (names separated by spaces, split the way the
    MCP SDK splits it) is too large to accept, or ``None``."""
    if scope is None:
        return None
    if len(scope) > MAX_SIGN_IN_TEXT:
        return f"longer than {MAX_SIGN_IN_TEXT} characters"
    if scope.count(" ") >= MAX_REGISTRATION_LIST:
        return f"more than {MAX_REGISTRATION_LIST} entries"
    return None


def _bounded_scopes(scopes: Iterable[str]) -> list[str]:
    """``scopes`` without repeats and without the platform's reserved
    ``mcpolis:`` names, in order, at most ``MAX_REGISTRATION_LIST`` of
    them: what a token may hold. A sign-in's scopes (``authorize``) and
    every minted token's (``_mint_tokens``) pass through here."""
    kept = strip_reserved_scopes(list(scopes))
    return list(dict.fromkeys(kept))[:MAX_REGISTRATION_LIST]


# The client address of the sign-in request being handled, as the rate
# limiter keys it (``ip_bucket_key``); "" when unknown (a direct call).
# ``RateLimitMiddleware`` sets it for the gateway's sign-in routes.
_request_source: ContextVar[str] = ContextVar(
    "gateway_sign_in_source", default="",
)


@contextlib.contextmanager
def sign_in_requests_from(source: str) -> Iterator[None]:
    """Count the unused registrations and pending sign-ins made inside
    this block against ``source`` (see ``MAX_PER_SOURCE``)."""
    reset_token = _request_source.set(source)
    try:
        yield
    finally:
        _request_source.reset(reset_token)


@dataclass(frozen=True)
class SignInLimits:
    """The caps on the sign-in state requests leave behind, per source
    and in total (see the constants). Tests pass smaller ones."""

    max_unused_registrations: int = MAX_UNUSED_REGISTRATIONS
    max_pending_sign_ins: int = MAX_PENDING_SIGN_INS
    max_unexchanged_codes: int = MAX_UNEXCHANGED_CODES
    max_pending_consents: int = MAX_PENDING_CONSENTS
    max_per_source: int = MAX_PER_SOURCE


@dataclass
class _Cap:
    """One cap, which logs that it pushed items out at most once per
    ``CLEANUP_INTERVAL``: a flood shows in the logs without flooding
    them. ``pushed_out`` counts since the previous line; ``source`` (for
    a per-source cap) is the one that reached it at that line."""

    what: str
    limit: int
    pushed_out: int = 0
    next_line_at: float = 0.0

    def note(self, count: int, now: float, source: str | None = None) -> None:
        if not count:
            return
        self.pushed_out += count
        if now < self.next_line_at:
            return
        fields: dict[str, object] = {
            "what": self.what, "limit": self.limit, "pushed_out": self.pushed_out,
        }
        if source is not None:
            fields["source"] = source
        logger.warning("gateway_oauth.cap_reached", **fields)
        self.pushed_out = 0
        self.next_line_at = now + CLEANUP_INTERVAL


def _over_caps(
    entries: Iterable[tuple[str, str]],
    newcomer: str | None,
    per_source: int,
    total: int,
) -> tuple[list[str], list[str]]:
    """The items to push out so that ``entries`` fit their caps, never
    ``newcomer`` (the item just added, or ``None``).

    ``entries`` are ``(key, source)``, oldest first. First the
    newcomer's source keeps at most ``per_source`` items: its oldest go.
    Then all of them keep at most ``total``: one at a time, the oldest
    item of the source holding the most goes. Of two holding as many, the
    newcomer's own source goes first: it is the one adding items, and
    another source's older item (a real person's pending sign-in, say)
    used to go before the flood's own. Else the one with the older oldest
    item. Returns the items pushed out by each of the two caps.
    """
    queues: dict[str, deque[str]] = {}
    position: dict[str, int] = {}
    newcomer_source: str | None = None
    for index, (key, source) in enumerate(entries):
        if key == newcomer:
            newcomer_source = source
            continue
        position[key] = index
        queues.setdefault(source, deque()).append(key)
    held = {source: len(keys) for source, keys in queues.items()}
    if newcomer_source is not None:
        held[newcomer_source] = held.get(newcomer_source, 0) + 1

    own: list[str] = []
    if newcomer_source is not None:
        mine = queues.get(newcomer_source)
        while mine and held[newcomer_source] > per_source:
            own.append(mine.popleft())
            held[newcomer_source] -= 1

    overall: list[str] = []
    excess = sum(held.values()) - total
    while excess > 0:
        candidates = [source for source, keys in queues.items() if keys]
        if not candidates:
            break
        largest = max(
            candidates,
            key=lambda source: (
                held[source],
                source == newcomer_source,
                -position[queues[source][0]],
            ),
        )
        overall.append(queues[largest].popleft())
        held[largest] -= 1
        excess -= 1
    return own, overall


@dataclass
class _Caps:
    """One kind's two caps (see ``MAX_PER_SOURCE``)."""

    per_source: _Cap
    total: _Cap

    def push_out(
        self,
        entries: Iterable[tuple[str, str]],
        newcomer: str | None,
        newcomer_source: str | None,
        now: float,
    ) -> list[str]:
        """The items of ``entries`` that must go (see ``_over_caps``),
        logging when a cap pushes some out."""
        own, overall = _over_caps(
            entries, newcomer, self.per_source.limit, self.total.limit,
        )
        self.per_source.note(len(own), now, source=newcomer_source)
        self.total.note(len(overall), now)
        return own + overall


class _Dated(Protocol):
    @property
    def created_at(self) -> float: ...


class _Sourced(_Dated, Protocol):
    @property
    def source(self) -> str: ...


def _drop_expired[T: _Dated](
    items: OrderedDict[str, T], ttl: float, now: float,
) -> None:
    """Drop the expired ones from ``items``, kept in the order they were
    created: oldest first, so it stops at the first one still valid."""
    while items and now - next(iter(items.values())).created_at > ttl:
        items.popitem(last=False)


def _add_within_caps[T: _Sourced](
    items: OrderedDict[str, T],
    key: str,
    item: T,
    ttl: float,
    caps: _Caps,
    now: float,
) -> None:
    """Add ``item`` to ``items`` (kept oldest first): drop the expired
    ones, then what the caps push out, never ``item`` itself."""
    _drop_expired(items, ttl, now)
    items[key] = item
    pushed_out = caps.push_out(
        ((other_key, other.source) for other_key, other in items.items()),
        key,
        item.source,
        now,
    )
    for other_key in pushed_out:
        del items[other_key]


def _is_abandoned(client: StoredClient, now: float) -> bool:
    """A registration that never received a token, past its window."""
    return (
        not client.token_issued
        and now - client.registered_at > UNUSED_REGISTRATION_TTL
    )


def _access_token_expired(token: StoredAccessToken, now: float) -> bool:
    return token.expires_at < now


def _refresh_token_expired(token: StoredRefreshToken, now: float) -> bool:
    return REFRESH_TOKEN_TTL is not None and now - token.created_at > REFRESH_TOKEN_TTL


def _token_response(
    access: StoredAccessToken, refresh: StoredRefreshToken,
) -> OAuthToken:
    return OAuthToken(
        access_token=access.token,
        token_type="Bearer",
        expires_in=ACCESS_TOKEN_TTL,
        refresh_token=refresh.token,
        scope=" ".join(access.scopes) if access.scopes else None,
    )


class _Kind(Enum):
    """The kinds of persistent items, one in-memory dict each."""

    client = "client"
    access_token = "access_token"
    refresh_token = "refresh_token"
    client_approval = "client_approval"


# One persistent item: its kind and its key in that kind's dict.
type _ItemKey = tuple[_Kind, str]


@dataclass
class PendingAuth:
    """State stored while user is authenticating with Google."""
    client: OAuthClientInformationFull
    params: AuthorizationParams
    google_state: str
    created_at: float = field(default_factory=time.time)
    # The address the ``/authorize`` request came from (see
    # ``MAX_PER_SOURCE``); "" when unknown.
    source: str = ""


@dataclass
class PendingConsent:
    """A Google-authenticated flow parked at the mcpolis consent page.

    Google has told us who the user is, but the client has not yet been
    approved by this user, so no auth code exists yet. Held in memory
    until the user approves or denies (or it expires).
    """
    client: OAuthClientInformationFull
    params: AuthorizationParams
    user_email: str
    created_at: float

    @property
    def source(self) -> str:
        """The member who signed in (see ``MAX_PER_SOURCE``)."""
        return email_key(self.user_email)


@dataclass
class ConsentPrompt:
    """What the consent page shows the user about the requesting client."""
    client_id: str
    client_name: str | None
    redirect_uri: str
    redirect_host: str


@dataclass
class StoredAuthCode:
    """Authorization code issued after Google login completes."""
    client_id: str
    user_email: str
    code_challenge: str
    redirect_uri: AnyUrl
    redirect_uri_provided_explicitly: bool
    scopes: list[str] | None
    state: str | None
    created_at: float
    expires_at: float  # required by MCP SDK's TokenHandler

    @property
    def source(self) -> str:
        """The member who signed in (see ``MAX_PER_SOURCE``)."""
        return email_key(self.user_email)


class McpGatewayOAuthProvider:
    """MCP gateway OAuth provider that delegates authentication to Google.

    Implements the MCP OAuthAuthorizationServerProvider protocol.
    MCP clients authenticate with MCPolis via OAuth 2.1. MCPolis delegates
    the actual "who are you?" to Google, gets the user's email, checks the
    policy, and issues its own tokens.

    Persistent state (registered clients, access tokens, refresh tokens,
    client approvals) lives behind an ``OAuthStateRepository`` — file in
    standalone mode, Mongo in cloud mode — and is global, not partitioned
    by org. Tokens identify a *user*; the org dimension is decided per
    request by the caller (the gateway controller fans out across the
    user's memberships, the admin MCP resolves it from the URL slug).

    All of it is kept in memory, loaded at startup; memory is the truth
    and storage follows item by item::

        every change to a client / token / approval:
            change memory, mark the item unsaved
            under the write lock: write each unsaved item's CURRENT memory
                state (present -> stored, absent -> deleted)
            failed write -> the item stays unsaved; a later write, or a
                retry after 1 s, 2 s, 4 s ... (at most 60 s), stores it

    One lock orders every write, and each write reads memory when it
    runs, so a write that started before a revoke can't land after the
    revoke's deletion and bring the tokens back. Grants (a new client,
    new tokens) are written before the caller gets them; on failure they
    are taken back out of memory, so nobody holds something storage will
    lose at the next restart. Deletions (revokes, expired items) happen
    in memory at once and reach storage in the background.

    Pending auths, auth codes and pending consents stay in memory only
    (short-lived, request-scoped).
    """

    def __init__(
        self,
        google_client_id: str,
        google_client_secret: str,
        server_url: str,
        runtime_manager: OrgRuntimeManager,
        state_repository: OAuthStateRepository,
        event_bus: EventStream | None = None,
        org_service: OrgService | None = None,
        limits: SignInLimits | None = None,
        dashboard_url: str | None = None,
        write_retry_delay: float = WRITE_RETRY_FIRST_DELAY,
    ) -> None:
        self.google_client_id = google_client_id
        self.google_client_secret = google_client_secret
        self.server_url = server_url
        # Where invited people accept their invitation (the gateway may
        # have a host of its own); the gateway's when not given.
        self._dashboard_url = (dashboard_url or server_url).rstrip("/")
        self._runtime_manager = runtime_manager
        self._event_bus = event_bus
        self._state_repo = state_repository
        self._limits = limits or SignInLimits()
        per_source = self._limits.max_per_source
        self._registration_caps = _Caps(
            _Cap("unused registrations from one address", per_source),
            _Cap("unused registrations", self._limits.max_unused_registrations),
        )
        self._pending_sign_in_caps = _Caps(
            _Cap("pending sign-ins from one address", per_source),
            _Cap("pending sign-ins", self._limits.max_pending_sign_ins),
        )
        self._code_caps = _Caps(
            _Cap("unexchanged codes of one member", per_source),
            _Cap("unexchanged codes", self._limits.max_unexchanged_codes),
        )
        self._consent_caps = _Caps(
            _Cap("pending consents of one member", per_source),
            _Cap("pending consents", self._limits.max_pending_consents),
        )
        # Used by ``handle_google_callback`` to check that the
        # authenticated user is a member of *some* org. Optional so
        # tests / standalone setups can construct the provider without
        # wiring the full org graph; when ``None``, the policy check
        # falls back to the legacy single-org runtime check.
        self._org_service = org_service

        # Ephemeral (in-memory only — state and code strings are
        # globally unique and short-lived). Each is kept in the order its
        # items were created, so the oldest is the first one.
        self._pending_auths: OrderedDict[str, PendingAuth] = OrderedDict()
        self._auth_codes: OrderedDict[str, StoredAuthCode] = OrderedDict()
        self._pending_consents: OrderedDict[str, PendingConsent] = OrderedDict()

        # Persistent state (single global namespace). Loaded lazily
        # via ``_ensure_loaded`` on first access.
        self._clients: dict[str, StoredClient] = {}
        self._access_tokens: dict[str, StoredAccessToken] = {}
        self._refresh_tokens: dict[str, StoredRefreshToken] = {}
        self._client_approvals: dict[str, StoredClientApproval] = {}
        # The address each registration made since startup came from,
        # while it has received no token (see ``MAX_PER_SOURCE``). Memory
        # only: a registration loaded from storage counts as from "".
        self._registration_sources: dict[str, str] = {}
        self._loaded = False
        self._load_lock = asyncio.Lock()
        # Items changed in memory whose stored copy may differ.
        self._unsaved: set[_ItemKey] = set()
        # Held by every write to storage (and by the change that
        # precedes it), so writes land in the order memory changed.
        self._write_lock = asyncio.Lock()
        self._next_cleanup_at = 0.0
        self._maintenance_scheduled = False
        # Background writes (``_schedule_maintenance``), held until
        # each ends.
        self._background_tasks = BackgroundTaskSet()
        # After a failed write: when unsaved items are written again
        # (``_retry_unsaved_later``), and the wait after the next failure.
        self._first_retry_delay = write_retry_delay
        self._next_retry_delay = write_retry_delay
        self._retry_timer: asyncio.TimerHandle | None = None

    def set_org_service(self, org_service: OrgService) -> None:
        """Attach the OrgService post-construction.

        Needed because the provider is built before ``OrgService`` is
        available in ``create_app``; this setter lets us wire it up
        once ``OrgService`` exists.
        """
        self._org_service = org_service

    # --- Persistence ---

    async def load_state(self) -> None:
        """Eagerly hydrate the in-memory state.

        Kept as a separate entrypoint so ``app_lifespan`` can warm the
        cache at startup; ``_ensure_loaded`` is the lazy guard used by
        every mutating / reading method.
        """
        await self._ensure_loaded()

    async def _ensure_loaded(self) -> None:
        """Load the stored state into memory on first access, and drop
        what expired while the backend was down.

        Cheap on subsequent calls — the loaded flag short-circuits
        before taking the lock. Never called with ``_write_lock`` held.
        """
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            snapshot = await self._state_repo.load()
            self._clients = dict(snapshot.clients)
            self._access_tokens = dict(snapshot.access_tokens)
            self._refresh_tokens = dict(snapshot.refresh_tokens)
            self._client_approvals = dict(snapshot.client_approvals)
            self._loaded = True
            # No field name may contain "token": the log redactor masks
            # the value of any such field.
            logger.info(
                "gateway_oauth.state.loaded",
                clients=len(self._clients),
                access=len(self._access_tokens),
                refresh=len(self._refresh_tokens),
                client_approvals=len(self._client_approvals),
            )
            self._expire(time.time())
            self._schedule_maintenance()

    async def flush(self) -> None:
        """Write every change storage does not have yet: deletions still
        on their way in the background (and the clean-up, when due), and
        earlier writes that failed.

        Called at shutdown, so a revoke made just before a deploy is not
        undone by the restart. Raises if some could not be written.
        """
        async with self._write_lock:
            self._expire(time.time())
            await self._write(set(self._unsaved))

    def _changes_for(self, keys: Iterable[_ItemKey]) -> OAuthStateChanges:
        """The current in-memory state of ``keys``: present items are
        stored, absent ones deleted."""
        changes = OAuthStateChanges()
        for kind, key in keys:
            match kind:
                case _Kind.client:
                    changes.clients[key] = self._clients.get(key)
                case _Kind.access_token:
                    changes.access_tokens[key] = self._access_tokens.get(key)
                case _Kind.refresh_token:
                    changes.refresh_tokens[key] = self._refresh_tokens.get(key)
                case _Kind.client_approval:
                    changes.client_approvals[key] = self._client_approvals.get(key)
        return changes

    async def _write(self, keys: set[_ItemKey]) -> None:
        """Store the current in-memory state of ``keys``.

        Caller holds ``_write_lock``. Raises if they were not all
        written; they then stay unsaved, and are written again later
        (``_retry_unsaved_later``), or by an earlier write.
        """
        if not keys:
            return
        self._unsaved -= keys
        try:
            await self._state_repo.apply(self._changes_for(keys))
        except BaseException:
            self._unsaved |= keys
            self._retry_unsaved_later()
            raise
        self._next_retry_delay = self._first_retry_delay

    def _retry_unsaved_later(self) -> None:
        """After a failed write: write what is unsaved again after a
        while, even if nothing else happens meanwhile. Each failure in a
        row doubles the wait, up to ``WRITE_RETRY_MAX_DELAY``."""
        if self._retry_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = self._next_retry_delay
        self._next_retry_delay = min(delay * 2, WRITE_RETRY_MAX_DELAY)
        self._retry_timer = loop.call_later(delay, self._retry_unsaved)

    def _retry_unsaved(self) -> None:
        self._retry_timer = None
        self._schedule_maintenance()

    def _drop(self, keys: Iterable[_ItemKey]) -> None:
        """Remove items from memory and mark them for deletion."""
        for kind, key in keys:
            match kind:
                case _Kind.client:
                    self._clients.pop(key, None)
                    self._registration_sources.pop(key, None)
                case _Kind.access_token:
                    self._access_tokens.pop(key, None)
                case _Kind.refresh_token:
                    self._refresh_tokens.pop(key, None)
                case _Kind.client_approval:
                    self._client_approvals.pop(key, None)
            self._unsaved.add((kind, key))

    def _forget(self, keys: set[_ItemKey]) -> None:
        """Remove items from memory now; storage follows in the
        background, after any write already under way."""
        self._drop(keys)
        self._schedule_maintenance()

    def _schedule_maintenance(self) -> None:
        """In the background: write what is unsaved, and delete expired
        items when the clean-up is due."""
        if self._maintenance_scheduled:
            return
        if not self._unsaved and time.time() < self._next_cleanup_at:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop — a unit test driving the provider synchronously.
            # The items stay unsaved; the next write stores them.
            return
        self._maintenance_scheduled = True
        self._background_tasks.spawn(self._maintain())

    async def _maintain(self) -> None:
        started = False
        try:
            async with self._write_lock:
                started = True
                # Changes made from here on schedule another run.
                self._maintenance_scheduled = False
                self._expire(time.time())
                keys = set(self._unsaved)
                try:
                    await self._write(keys)
                except Exception:
                    logger.warning(
                        "gateway_oauth.store.write_failed",
                        items=len(keys),
                        exc_info=True,
                    )
        finally:
            if not started:
                self._maintenance_scheduled = False

    def _expire(self, now: float) -> None:
        """When the clean-up is due, drop expired tokens and abandoned
        registrations (with their approvals) from memory, then the
        unused registrations beyond the cap (loaded from storage over
        it); the next write deletes them from storage. Also drops
        expired pending sign-ins, codes and consent pages (memory only)."""
        if now < self._next_cleanup_at:
            return
        self._next_cleanup_at = now + CLEANUP_INTERVAL
        abandoned = {
            client_id
            for client_id, client in self._clients.items()
            if _is_abandoned(client, now)
        }
        expired = self._client_items(abandoned)
        expired |= {
            (_Kind.access_token, token)
            for token, stored in self._access_tokens.items()
            if _access_token_expired(stored, now)
        }
        expired |= {
            (_Kind.refresh_token, token)
            for token, stored_r in self._refresh_tokens.items()
            if _refresh_token_expired(stored_r, now)
        }
        if expired:
            counts = {kind: 0 for kind in _Kind}
            for kind, _key in expired:
                counts[kind] += 1
            self._drop(expired)
            logger.info(
                "gateway_oauth.expired_removed",
                clients=counts[_Kind.client],
                access=counts[_Kind.access_token],
                refresh=counts[_Kind.refresh_token],
                client_approvals=counts[_Kind.client_approval],
            )
        self._cap_unused_registrations(now)
        # The short-lived sign-in state, between the drops each new item
        # makes (``_add_within_caps``).
        _drop_expired(self._pending_auths, PENDING_SIGN_IN_TTL, now)
        _drop_expired(self._auth_codes, AUTH_CODE_TTL, now)
        _drop_expired(self._pending_consents, CONSENT_TTL, now)

    def _client_items(self, client_ids: set[str]) -> set[_ItemKey]:
        """These clients and their approvals, as items."""
        items: set[_ItemKey] = {
            (_Kind.client, client_id) for client_id in client_ids
        }
        items |= {
            (_Kind.client_approval, key)
            for key, approval in self._client_approvals.items()
            if approval.client_id in client_ids
        }
        return items

    def _cap_unused_registrations(
        self, now: float, newcomer: str | None = None,
    ) -> None:
        """Drop the registrations that never received a token (with their
        approvals) beyond their caps (see ``MAX_PER_SOURCE``), never
        ``newcomer``; the next write deletes them from storage."""
        unused = sorted(
            (client.registered_at, client_id)
            for client_id, client in self._clients.items()
            if not client.token_issued
        )
        pushed_out = self._registration_caps.push_out(
            (
                (client_id, self._registration_sources.get(client_id, ""))
                for _registered_at, client_id in unused
            ),
            newcomer,
            self._registration_sources.get(newcomer, "") if newcomer else None,
            now,
        )
        if pushed_out:
            self._drop(self._client_items(set(pushed_out)))

    # --- Client registration ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        await self._ensure_loaded()
        stored = self._clients.get(client_id)
        if stored is None or _is_abandoned(stored, time.time()):
            return None
        return stored.info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None
        self._assert_within_registration_limits(client_info)
        self._assert_no_reserved_scopes(client_info)
        self._assert_registrable_redirect_uris(client_info)
        await self._ensure_loaded()
        client_id = client_info.client_id
        async with self._write_lock:
            earlier = self._clients.get(client_id)
            self._clients[client_id] = StoredClient(
                info=client_info,
                registered_at=earlier.registered_at if earlier else time.time(),
                token_issued=earlier.token_issued if earlier else False,
            )
            try:
                await self._write({(_Kind.client, client_id)})
            except BaseException:
                # Not stored, so not registered (the next write deletes
                # whatever part did land).
                if earlier is None:
                    self._clients.pop(client_id, None)
                else:
                    self._clients[client_id] = earlier
                raise
            if not self._clients[client_id].token_issued:
                self._registration_sources.setdefault(
                    client_id, _request_source.get(),
                )
            # Room for this one: unused registrations beyond the caps
            # go, from storage too (in the background, below).
            self._cap_unused_registrations(time.time(), newcomer=client_id)
        self._schedule_maintenance()

    @staticmethod
    def _assert_within_registration_limits(
        client_info: OAuthClientInformationFull,
    ) -> None:
        """Refuse a registration holding an oversized value (HTTP 400,
        ``invalid_client_metadata``). The request body itself is capped
        before parsing (``MAX_SIGN_IN_REQUEST_BYTES``)."""
        fields = client_info.model_dump(mode="json", exclude_none=True)
        for name, value in fields.items():
            problem = (
                _scope_problem(client_info.scope)
                if name == "scope"
                else _size_problem(value)
            )
            if problem is not None:
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description=f"{name} is {problem}",
                )

    @staticmethod
    def _assert_no_reserved_scopes(
        client_info: OAuthClientInformationFull,
    ) -> None:
        """Refuse a registration asking for the platform's ``mcpolis:``
        scope namespace (HTTP 400, ``invalid_client_metadata``).

        Registration is open to anyone, so a scope is client input. Such
        scopes are also stripped from everything stored or loaded
        (``_bounded_scopes``, ``load_access_token``), which covers
        clients and tokens stored before this refusal existed.
        """
        reserved = [
            s for s in (client_info.scope or "").split() if is_reserved_scope(s)
        ]
        if not reserved:
            return
        # An attack signal: no legitimate client asks for these.
        logger.warning(
            "oauth.register.reserved_scope_refused",
            reserved_scope_count=len(reserved),
            reserved_scopes=reserved[:5],
        )
        raise RegistrationError(
            error="invalid_client_metadata",
            error_description=f"Reserved scopes: {', '.join(reserved[:5])}",
        )

    @staticmethod
    def _assert_registrable_redirect_uris(
        client_info: OAuthClientInformationFull,
    ) -> None:
        """Reject a registration carrying a cleartext-remote redirect URI.

        Raised as the SDK's ``RegistrationError`` so the registration
        handler renders a clean 400 (``invalid_redirect_uri``) rather than
        letting the exception escape as a 500. One bad URI fails the whole
        registration — an attacker can't smuggle one in beside a good one.
        """
        for uri in client_info.redirect_uris or []:
            if not _is_registrable_redirect_uri(uri):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=(
                        "redirect_uri must be https, a loopback http "
                        "address, or a private-use URI scheme; cleartext "
                        f"http to a remote host is not allowed: {uri}"
                    ),
                )

    # --- Authorization ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Redirect to Google's OAuth consent screen."""
        self._assert_within_authorize_limits(params)
        google_state = secrets.token_urlsafe(32)
        await self._ensure_loaded()

        logger.info(
            "oauth.authorize.started",
            client_name=client.client_name,
            redirect_uri=str(params.redirect_uri),
            oauth_state=params.state,
        )

        if params.scopes is not None:
            # Kept with the sign-in, then with its code and tokens.
            params = params.model_copy(
                update={"scopes": _bounded_scopes(params.scopes)},
            )
        now = time.time()
        _add_within_caps(
            self._pending_auths,
            google_state,
            PendingAuth(
                client=client,
                params=params,
                google_state=google_state,
                created_at=now,
                source=_request_source.get(),
            ),
            PENDING_SIGN_IN_TTL,
            self._pending_sign_in_caps,
            now,
        )

        callback_url = f"{self.server_url.rstrip('/')}/mcp/oauth/google/callback"

        google_params = {
            "client_id": self.google_client_id,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": "openid email",
            "state": google_state,
            "access_type": "online",
            "prompt": "select_account",
        }

        return f"{GOOGLE_AUTH_URL}?{urlencode(google_params)}"

    @staticmethod
    def _assert_within_authorize_limits(params: AuthorizationParams) -> None:
        """Refuse an ``/authorize`` request carrying a value too large to
        keep with the pending sign-in. The SDK sends the client back to
        its redirect URI with ``invalid_request``.

        The redirect URI is bounded already: it must be one the client
        registered, each at most ``MAX_SIGN_IN_TEXT``. The scopes are
        not: the SDK only checks that each one asked for is registered,
        so a name repeated a million times passes. They are bounded here,
        before anything is kept."""
        scopes = None if params.scopes is None else " ".join(params.scopes)
        for name, problem in (
            ("state", _size_problem(params.state)),
            ("code_challenge", _size_problem(params.code_challenge)),
            ("resource", _size_problem(params.resource)),
            ("scope", _scope_problem(scopes)),
        ):
            if problem is not None:
                raise AuthorizeError(
                    error="invalid_request",
                    error_description=f"{name} is {problem}",
                )

    async def _is_user_authorized(self, email: str) -> bool:
        """Return True if the user is allowed to authenticate to MCPolis.

        Cloud (with OrgService): the user must be a member of at least
        one org.

        Legacy / standalone (no OrgService): fall back to the
        runtime-policy check on the cached default-org runtime — the
        provider was previously constructed without an org service and
        many tests rely on this path. Empty policy = open to
        authenticate. This is deliberately NOT tied to the
        PolicyEngine's access semantics (where zero roles now means
        zero tools): this method gates who may complete the OAuth
        flow, not what they can reach afterwards. An identity that
        authenticates against an empty-policy runtime still resolves
        to no role and gets an empty tool list, so the open
        authentication grants no access.
        """
        if self._org_service is not None:
            orgs = await self._org_service.list_user_orgs(email)
            return bool(orgs)

        from mcpolis.domain.ports import DEFAULT_ORG_ID

        runtime = self._runtime_manager.get_cached(DEFAULT_ORG_ID)
        if runtime is None or runtime.policy_engine.is_empty:
            return True
        return bool(runtime.policy_engine.get_user_roles(email))

    async def _refusal(self, email: str) -> str:
        """What a person ``_is_user_authorized`` refuses is told. An
        invited person who has not accepted yet is sent to the Join page
        of the invitation: signing in from an AI client never accepts
        one (only a click on Join does)."""
        invitations: list[Invitation] = []
        if self._org_service is not None:
            try:
                invitations = await self._org_service.list_invitations(email)
            except Exception:
                # Only the wording of the refusal depends on it.
                logger.warning(
                    "oauth.refusal.list_invitations_failed", exc_info=True,
                )
        if not invitations:
            return (
                f"Access denied: {email} is not authorized to use this "
                "MCP Hero instance"
            )
        org = invitations[0].org
        return (
            f"Access denied: {email} is invited to {org.display_name} but "
            "has not joined it yet. To join, open "
            f"{self._dashboard_url}/orgs/{org.slug}/join and click Join, "
            "then connect from your AI client again."
        )

    async def handle_google_callback(
        self, code: str, state: str
    ) -> str:
        """Handle Google's redirect callback.

        Exchanges Google's code for an ID token, extracts the email,
        checks the policy, generates an MCPolis auth code, and returns
        the redirect URL back to the MCP client.

        Returns the redirect URL for the MCP client.
        Raises ValueError if the user is not authorized.
        """
        pending = self._pending_auths.pop(state, None)
        if (
            pending is None
            or time.time() - pending.created_at > PENDING_SIGN_IN_TTL
        ):
            raise ValueError("Invalid or expired OAuth state")

        # Exchange Google code for tokens. A misbehaving token endpoint
        # (5xx, transport timeout) is a third-party fault we must *handle*:
        # map any httpx failure to the same ValueError family the no-id_token
        # path raises, so the callback route renders a clean error instead of
        # letting a raw httpx exception escape as an unhandled 500.
        callback_url = f"{self.server_url.rstrip('/')}/mcp/oauth/google/callback"
        try:
            async with httpx.AsyncClient() as http_client:
                resp = await http_client.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": self.google_client_id,
                        "client_secret": self.google_client_secret,
                        "redirect_uri": callback_url,
                        "grant_type": "authorization_code",
                    },
                )
                resp.raise_for_status()
                token_data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            # httpx.HTTPError covers status (raise_for_status) + transport
            # (timeout/connect) faults; json.JSONDecodeError covers a 200
            # with a malformed body. Map all of them to the clean ValueError
            # family the no-id_token path raises — don't rely on the
            # callback route's ``except ValueError`` happening to catch a
            # raw JSONDecodeError.
            logger.warning(
                "gateway_oauth.google_token_exchange_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ValueError(
                "Google token exchange failed; please retry authentication"
            ) from exc

        # Extract email from ID token (Google returns a JWT)
        id_token = token_data.get("id_token")
        if not id_token:
            raise ValueError("Google did not return an ID token")

        email = extract_email_from_id_token(id_token)
        if not email:
            raise ValueError("Could not extract email from Google ID token")

        if not await self._is_user_authorized(email):
            raise ValueError(await self._refusal(email))

        # Confused-deputy gate. Google has told us *who* the user is, but
        # a client this user has not approved before must not receive an
        # authorization code silently — that is exactly how a hostile
        # registered client would ride the victim's Google login. Park the
        # flow at the mcpolis consent page; the code is minted only once
        # the user approves (``resolve_consent``). An already-approved
        # (user, client) pair is forwarded directly, so real clients
        # prompt once.
        assert pending.client.client_id is not None
        if not await self.is_client_approved(
            email,
            pending.client.client_id,
            str(pending.params.redirect_uri),
        ):
            # A consent page left unanswered is otherwise only dropped
            # when its own token is used again.
            now = time.time()
            consent_token = secrets.token_urlsafe(32)
            _add_within_caps(
                self._pending_consents,
                consent_token,
                PendingConsent(
                    client=pending.client,
                    params=pending.params,
                    user_email=email,
                    created_at=now,
                ),
                CONSENT_TTL,
                self._consent_caps,
                now,
            )
            logger.info(
                "oauth.consent.required",
                client_id=pending.client.client_id,
                client_name=pending.client.client_name,
                user_email=email,
                redirect_uri=str(pending.params.redirect_uri),
            )
            return self._build_consent_url(consent_token)

        return self._issue_code_and_redirect(
            pending.client, pending.params, email
        )

    # --- Consent (confused-deputy gate) ---

    @staticmethod
    def _approval_key(
        user_email: str, client_id: str, redirect_uri: str
    ) -> str:
        return client_approval_key(
            user_email, client_id, _redirect_identity(redirect_uri),
        )

    async def is_client_approved(
        self, user_email: str, client_id: str, redirect_uri: str
    ) -> bool:
        """Whether *user_email* has already approved *client_id* for the
        ``scheme://host`` of *redirect_uri*. A different host for the same
        client re-prompts — the approval binds to the host the user saw."""
        await self._ensure_loaded()
        key = self._approval_key(user_email, client_id, redirect_uri)
        return key in self._client_approvals

    async def record_client_approval(
        self, user_email: str, client_id: str, redirect_uri: str
    ) -> None:
        """Remember that *user_email* approved *client_id* for the
        ``scheme://host`` of *redirect_uri*, so future authorizations for
        that exact (user, client, host) skip the consent page."""
        await self._ensure_loaded()
        approval = StoredClientApproval(
            user_email=user_email,
            client_id=client_id,
            redirect_identity=_redirect_identity(redirect_uri),
            approved_at=time.time(),
        )
        async with self._write_lock:
            self._client_approvals[approval.key] = approval
            try:
                await self._write({(_Kind.client_approval, approval.key)})
            except Exception:
                # Remembered all the same: it stays unsaved and the next
                # write stores it. Lost only if the backend restarts
                # first, which shows the consent page once more.
                logger.warning(
                    "gateway_oauth.store.write_failed", items=1, exc_info=True,
                )
        self._schedule_maintenance()

    def _build_consent_url(self, consent_token: str) -> str:
        return (
            f"{self.server_url.rstrip('/')}/mcp/oauth/consent"
            f"?{urlencode({'consent': consent_token})}"
        )

    async def render_consent(self, consent_token: str) -> ConsentPrompt | None:
        """Describe the client awaiting consent, for the consent page.

        A read-only peek — does NOT consume the token (the approve/deny
        POST does). Returns ``None`` for an unknown or expired token.
        """
        pending = self._pending_consents.get(consent_token)
        if pending is None:
            return None
        if time.time() - pending.created_at > CONSENT_TTL:
            self._pending_consents.pop(consent_token, None)
            return None
        redirect_uri = str(pending.params.redirect_uri)
        assert pending.client.client_id is not None
        return ConsentPrompt(
            client_id=pending.client.client_id,
            client_name=pending.client.client_name,
            redirect_uri=redirect_uri,
            redirect_host=_redirect_identity(redirect_uri),
        )

    async def resolve_consent(self, consent_token: str, approve: bool) -> str:
        """Apply the user's approve/deny decision for a parked flow.

        Approve → remember the (user, client) approval, mint the auth
        code, and return the client redirect carrying it. Deny → mint
        nothing and return the client redirect carrying
        ``error=access_denied``. Single-use: the token is consumed here,
        so a replay can't mint a second code.

        Raises ``ValueError`` for an unknown, expired, or already-used
        token.
        """
        pending = self._pending_consents.pop(consent_token, None)
        if pending is None or time.time() - pending.created_at > CONSENT_TTL:
            raise ValueError("Invalid or expired consent token")

        assert pending.client.client_id is not None
        if not approve:
            logger.info(
                "oauth.consent.denied",
                client_id=pending.client.client_id,
                user_email=pending.user_email,
            )
            return self._redirect_to_client(
                pending.params, {"error": "access_denied"}
            )

        await self.record_client_approval(
            pending.user_email,
            pending.client.client_id,
            str(pending.params.redirect_uri),
        )
        if not await self.is_client_approved(
            pending.user_email,
            pending.client.client_id,
            str(pending.params.redirect_uri),
        ):
            # A revoke landed while the approval was being saved: it
            # removed the approval, so no code either.
            raise ValueError("Invalid or expired consent token")
        logger.info(
            "oauth.consent.approved",
            client_id=pending.client.client_id,
            user_email=pending.user_email,
        )
        return self._issue_code_and_redirect(
            pending.client, pending.params, pending.user_email
        )

    def _issue_code_and_redirect(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
        user_email: str,
    ) -> str:
        """Mint a one-time auth code for the client and build its redirect.

        The single place a gateway auth code is created after identity is
        known — the approved-callback path and the consent-approve path
        both funnel through here.
        """
        auth_code = secrets.token_urlsafe(32)
        now = time.time()
        assert client.client_id is not None
        # A code never exchanged is otherwise only dropped when someone
        # presents it again.
        _add_within_caps(
            self._auth_codes,
            auth_code,
            StoredAuthCode(
                client_id=client.client_id,
                user_email=user_email,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                scopes=params.scopes,
                state=params.state,
                created_at=now,
                expires_at=now + AUTH_CODE_TTL,
            ),
            AUTH_CODE_TTL,
            self._code_caps,
            now,
        )
        return self._redirect_to_client(params, {"code": auth_code})

    @staticmethod
    def _redirect_to_client(
        params: AuthorizationParams, extra: dict[str, str]
    ) -> str:
        """Build a redirect back to the client's redirect_uri carrying
        ``extra`` plus the original ``state`` when present."""
        query = dict(extra)
        if params.state:
            query["state"] = params.state
        redirect_uri = str(params.redirect_uri)
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(query)}"

    # --- Token exchange ---

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> StoredAuthCode | None:
        stored = self._auth_codes.get(authorization_code)
        if stored is None:
            return None
        if stored.client_id != client.client_id:
            return None
        if time.time() - stored.created_at > AUTH_CODE_TTL:
            self._auth_codes.pop(authorization_code, None)
            return None
        return stored

    def _take_auth_code(self, authorization_code: StoredAuthCode) -> bool:
        """Consume a code (one-time use). False when it is gone: already
        exchanged, or revoked since the SDK loaded it."""
        for code, stored in self._auth_codes.items():
            if stored is authorization_code:
                del self._auth_codes[code]
                return True
        return False

    def _mint_tokens(
        self, client_id: str, user_email: str, scopes: list[str],
    ) -> tuple[StoredAccessToken, StoredRefreshToken]:
        """Add a new access + refresh token pair to memory (not yet
        stored). The pair holds ``scopes`` without repeats, at most
        ``MAX_REGISTRATION_LIST``: a refresh may ask for a token's scopes
        any number of times (the SDK only checks each is in the token),
        and every token is stored and loaded at each startup."""
        scopes = _bounded_scopes(scopes)
        now = time.time()
        access = StoredAccessToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            user_email=user_email,
            scopes=scopes,
            expires_at=int(now) + ACCESS_TOKEN_TTL,
        )
        refresh = StoredRefreshToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            user_email=user_email,
            scopes=scopes,
            created_at=now,
        )
        self._access_tokens[access.token] = access
        self._refresh_tokens[refresh.token] = refresh
        return access, refresh

    def _mark_token_issued(
        self, client: OAuthClientInformationFull,
    ) -> set[_ItemKey]:
        """Record that ``client`` received a token, so its registration
        is kept for good. Returns the client's key when that changed."""
        client_id = client.client_id
        assert client_id is not None
        stored = self._clients.get(client_id)
        if stored is not None and stored.token_issued:
            return set()
        # No longer counted against its address (``MAX_PER_SOURCE``).
        self._registration_sources.pop(client_id, None)
        self._clients[client_id] = StoredClient(
            info=stored.info if stored is not None else client,
            registered_at=(
                stored.registered_at if stored is not None else time.time()
            ),
            token_issued=True,
        )
        return {(_Kind.client, client_id)}

    async def _issue_tokens(
        self,
        client: OAuthClientInformationFull,
        user_email: str,
        scopes: list[str],
    ) -> tuple[StoredAccessToken, StoredRefreshToken]:
        """Mint a token pair for ``user_email`` and store it before
        anyone gets it. Caller holds ``_write_lock``. On a failed write
        the pair is taken back out of memory and the error raised: a
        client must never hold tokens that a restart would forget."""
        assert client.client_id is not None
        access, refresh = self._mint_tokens(client.client_id, user_email, scopes)
        keys: set[_ItemKey] = {
            (_Kind.access_token, access.token),
            (_Kind.refresh_token, refresh.token),
        }
        keys |= self._mark_token_issued(client)
        try:
            await self._write(keys)
        except BaseException:
            # Unsaved and absent: the next write deletes any part that
            # did land.
            self._access_tokens.pop(access.token, None)
            self._refresh_tokens.pop(refresh.token, None)
            raise
        return access, refresh

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: StoredAuthCode
    ) -> OAuthToken:
        assert client.client_id is not None
        await self._ensure_loaded()
        async with self._write_lock:
            if not self._take_auth_code(authorization_code):
                raise TokenError(
                    error="invalid_grant",
                    error_description="authorization code does not exist",
                )
            access, refresh = await self._issue_tokens(
                client,
                authorization_code.user_email,
                authorization_code.scopes or [],
            )
        self._schedule_maintenance()
        await self._publish_gateway_connected(authorization_code.user_email)
        return _token_response(access, refresh)

    # --- Token verification ---

    async def load_access_token(self, token: str) -> AccessToken | None:
        await self._ensure_loaded()
        stored = self._access_tokens.get(token)
        if stored is None:
            return None
        if _access_token_expired(stored, int(time.time())):
            self._forget({(_Kind.access_token, token)})
            return None
        # client_id is set to the user's email so the MCP SDK's
        # AuthenticatedUser.username becomes the email
        return AccessToken(
            token=token,
            client_id=stored.user_email,
            scopes=strip_reserved_scopes(stored.scopes),
            expires_at=stored.expires_at,
        )

    # --- Refresh tokens ---

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefreshToken | None:
        await self._ensure_loaded()
        stored = self._refresh_tokens.get(refresh_token)
        if stored is None:
            return None
        if stored.client_id != client.client_id:
            return None
        if _refresh_token_expired(stored, time.time()):
            self._forget({(_Kind.refresh_token, refresh_token)})
            return None
        return stored

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        await self._ensure_loaded()
        effective_scopes = scopes if scopes else refresh_token.scopes
        old_key: _ItemKey = (_Kind.refresh_token, refresh_token.token)
        async with self._write_lock:
            # Gone means already used (rotation), revoked, or expired
            # since the SDK loaded it: none may mint a new pair.
            if refresh_token.token not in self._refresh_tokens:
                raise TokenError(
                    error="invalid_grant",
                    error_description="refresh token does not exist",
                )
            # The new pair is stored before the old token is retired, so
            # a failed write leaves the client's current token working.
            access, refresh = await self._issue_tokens(
                client, refresh_token.user_email, effective_scopes,
            )
            # Rotation. If this delete is not stored, the token stays
            # unsaved and the next write retries it; the client already
            # holds the new pair, so its sign-in is safe either way.
            self._drop({old_key})
            try:
                await self._write({old_key})
            except Exception:
                logger.warning(
                    "gateway_oauth.store.write_failed", items=1, exc_info=True,
                )
        self._schedule_maintenance()
        await self._publish_gateway_connected(refresh_token.user_email)
        return _token_response(access, refresh)

    # --- Connected users ---

    def get_connected_users(self) -> list[str]:
        """Return emails of users with valid gateway tokens (global).

        Tokens are user-scoped, so this is a flat list of users who
        have authenticated to MCPolis at all. Per-org filtering (e.g.
        "users in this org with active tokens") is the caller's job —
        intersect this list with the org's membership roll.
        """
        now = time.time()
        emails: set[str] = set()
        for stored in self._access_tokens.values():
            if not _access_token_expired(stored, now):
                emails.add(stored.user_email)
        for stored_r in self._refresh_tokens.values():
            if not _refresh_token_expired(stored_r, now):
                emails.add(stored_r.user_email)
        return sorted(emails)

    def revoke_user_tokens(self, email: str) -> int:
        """End a user's gateway sign-in everywhere (tokens are global).

        Revokes their access and refresh tokens, and what would let them
        sign straight back in: an authorization code not yet exchanged,
        a consent page still open, and their client approvals (the next
        sign-in asks for consent again). Effective in memory at once;
        storage follows in the background, after any write already
        under way, so that write can't bring the tokens back. Returns
        how many tokens were revoked.

        ``email`` matches whatever its letter case (``same_email``): the
        tokens carry Google's spelling, an admin may type another.
        """
        tokens: set[_ItemKey] = {
            (_Kind.access_token, token)
            for token, stored in self._access_tokens.items()
            if same_email(stored.user_email, email)
        }
        tokens |= {
            (_Kind.refresh_token, token)
            for token, stored_r in self._refresh_tokens.items()
            if same_email(stored_r.user_email, email)
        }
        approvals: set[_ItemKey] = {
            (_Kind.client_approval, key)
            for key, approval in self._client_approvals.items()
            if same_email(approval.user_email, email)
        }
        for code in [
            code for code, stored_code in self._auth_codes.items()
            if same_email(stored_code.user_email, email)
        ]:
            del self._auth_codes[code]
        for consent in [
            consent for consent, pending in self._pending_consents.items()
            if same_email(pending.user_email, email)
        ]:
            del self._pending_consents[consent]
        self._forget(tokens | approvals)
        return len(tokens)

    # --- TokenVerifier protocol ---

    async def verify_token(self, token: str) -> AccessToken | None:
        """Implements the TokenVerifier protocol for BearerAuthBackend."""
        return await self.load_access_token(token)

    # --- Test-only token minting ---

    async def mint_test_token(
        self, user_email: str,
    ) -> str:
        """Mint a gateway access token for ``user_email``.

        Bypasses the Google OAuth round-trip — the caller is trusted
        (guarded upstream by ``MCPOLIS_TEST_MODE``). Registers a stable
        test client on first use so refresh flows have something to
        look up. Returns the access-token string.
        """
        await self._ensure_loaded()
        stored = self._clients.get(TEST_CLIENT_ID)
        test_client = (
            stored.info
            if stored is not None
            else OAuthClientInformationFull(
                client_id=TEST_CLIENT_ID,
                redirect_uris=[AnyUrl("http://localhost/test-callback")],
            )
        )
        async with self._write_lock:
            access, _refresh = await self._issue_tokens(test_client, user_email, [])
        self._schedule_maintenance()
        return access.token

    # --- Revocation ---

    async def revoke_token(
        self,
        token: StoredAccessToken | StoredRefreshToken,
    ) -> None:
        await self._ensure_loaded()
        kind = (
            _Kind.access_token
            if isinstance(token, StoredAccessToken)
            else _Kind.refresh_token
        )
        self._forget({(kind, token.token)})

    # --- Internals ---

    async def _publish_gateway_connected(self, user_email: str) -> None:
        """Publish a gateway_connected event for each org the user is in.

        The dashboard's org-scoped event stream subscribes per org;
        publishing per-org is what lets each org's connected-user count
        reflect this user. Best-effort — failures are logged and
        swallowed so OAuth flow doesn't break on event-bus problems.
        """
        if self._event_bus is None:
            return
        if self._org_service is None:
            from mcpolis.domain.ports import DEFAULT_ORG_ID

            self._event_bus.publish(DEFAULT_ORG_ID, Event(
                type="gateway_connected",
                user_email=user_email,
            ))
            return
        try:
            orgs = await self._org_service.list_user_orgs(user_email)
        except Exception:
            logger.exception("oauth.publish_connected.list_orgs.failed")
            return
        for org in orgs:
            try:
                self._event_bus.publish(org.id, Event(
                    type="gateway_connected",
                    user_email=user_email,
                ))
            except Exception:
                logger.exception(
                    "oauth.publish_connected.failed",
                    org_id=org.id,
                )


def extract_email_from_id_token(id_token: str) -> str | None:
    """Extract email from a Google ID token (JWT) without cryptographic verification.

    We trust this token because we just received it directly from Google's
    token endpoint over HTTPS in exchange for a code we generated.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return None

    # Decode the payload (second part)
    payload = parts[1]
    # Add padding
    payload += "=" * (4 - len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
        return claims.get("email")  # type: ignore[no-any-return]
    except Exception:
        logger.exception("oauth.google_id_token.decode.failed")
        return None
