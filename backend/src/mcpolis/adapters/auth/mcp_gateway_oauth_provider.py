"""MCP gateway OAuth Authorization Server Provider — issues bearer tokens to MCP clients, delegating identity to Google.

The dashboard's browser-login flow (cookie-issuing) lives in a sibling
adapter ``adapters/auth/google_oauth_provider.py`` and uses a different
port; this module is exclusively for the MCP gateway surface.
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx
import structlog
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from mcpolis.domain.model.events import Event
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateRepository,
    OAuthStateSnapshot,
    StoredAccessToken,
    StoredRefreshToken,
)

if TYPE_CHECKING:
    from mcpolis.domain.services.org_runtime import OrgRuntimeManager
    from mcpolis.domain.services.org_service import OrgService

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Token lifetimes
ACCESS_TOKEN_TTL = 3600  # 1 hour
# Refresh tokens expire after 30 days and are rotated on every use (the
# old token is removed and a new one is minted during
# ``exchange_refresh_token``). Set to ``None`` to disable expiry entirely
# (not recommended outside local development).
REFRESH_TOKEN_TTL: int | None = 30 * 86400  # 30 days


@dataclass
class PendingAuth:
    """State stored while user is authenticating with Google."""
    client: OAuthClientInformationFull
    params: AuthorizationParams
    google_state: str


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


class McpGatewayOAuthProvider:
    """MCP gateway OAuth provider that delegates authentication to Google.

    Implements the MCP OAuthAuthorizationServerProvider protocol.
    MCP clients authenticate with MCPolis via OAuth 2.1. MCPolis delegates
    the actual "who are you?" to Google, gets the user's email, checks the
    policy, and issues its own tokens.

    Persistent state (registered clients, access tokens, refresh tokens)
    lives behind an ``OAuthStateRepository`` — file in standalone mode,
    Mongo in cloud mode — and is global, not partitioned by org. Tokens
    identify a *user*; the org dimension is decided per request by the
    caller (the gateway controller fans out across the user's
    memberships, the admin MCP resolves it from the URL slug).

    Pending auths and auth codes stay in-memory only (short-lived,
    request-scoped).
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
    ) -> None:
        self.google_client_id = google_client_id
        self.google_client_secret = google_client_secret
        self.server_url = server_url
        self._runtime_manager = runtime_manager
        self._event_bus = event_bus
        self._state_repo = state_repository
        # Used by ``handle_google_callback`` to check that the
        # authenticated user is a member of *some* org. Optional so
        # tests / standalone setups can construct the provider without
        # wiring the full org graph; when ``None``, the policy check
        # falls back to the legacy single-org runtime check.
        self._org_service = org_service

        # Ephemeral (in-memory only — state and code strings are
        # globally unique and short-lived).
        self._pending_auths: dict[str, PendingAuth] = {}
        self._auth_codes: dict[str, StoredAuthCode] = {}

        # Persistent state (single global namespace). Loaded lazily
        # via ``_ensure_loaded`` on first access.
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._access_tokens: dict[str, StoredAccessToken] = {}
        self._refresh_tokens: dict[str, StoredRefreshToken] = {}
        self._loaded = False
        self._load_lock = asyncio.Lock()

    def set_org_service(self, org_service: OrgService) -> None:
        """Attach the OrgService post-construction.

        Needed because the provider is built before ``OrgService`` is
        available in ``create_app``; this setter lets us wire it up
        once ``OrgService`` exists.
        """
        self._org_service = org_service

    # --- Persistence ---

    async def load_state(self) -> None:
        """Eagerly hydrate the in-memory snapshot.

        Kept as a separate entrypoint so ``app_lifespan`` can warm the
        cache at startup; ``_ensure_loaded`` is the lazy guard used by
        every mutating / reading method.
        """
        await self._ensure_loaded()

    async def _ensure_loaded(self) -> None:
        """Load the persisted snapshot into memory on first access.

        Cheap on subsequent calls — the loaded flag short-circuits
        before taking the lock.
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
            self._loaded = True

    def _snapshot(self) -> OAuthStateSnapshot:
        return OAuthStateSnapshot(
            clients=dict(self._clients),
            access_tokens=dict(self._access_tokens),
            refresh_tokens=dict(self._refresh_tokens),
        )

    async def _save_state(self) -> None:
        await self._state_repo.save(self._snapshot())

    def _schedule_save(self) -> None:
        """Fire-and-forget save used by sync code paths.

        The MCP SDK's OAuth provider interface is sync-looking in many
        places (``revoke_token``, ``get_connected_users`` helpers) but
        we still need to durably persist. Scheduling on the running
        loop lets those callers not have to ``await``.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop — probably a unit test constructing the provider
            # directly. Skipping persistence is safe: the test will
            # either not care, or will await ``_save_state``
            # directly.
            return
        loop.create_task(self._save_state())

    # --- Client registration ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        await self._ensure_loaded()
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None
        await self._ensure_loaded()
        self._clients[client_info.client_id] = client_info
        await self._save_state()

    # --- Authorization ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Redirect to Google's OAuth consent screen."""
        google_state = secrets.token_urlsafe(32)
        await self._ensure_loaded()

        logger.info(
            "oauth.authorize.started",
            client_name=client.client_name,
            redirect_uri=str(params.redirect_uri),
            oauth_state=params.state,
        )

        self._pending_auths[google_state] = PendingAuth(
            client=client,
            params=params,
            google_state=google_state,
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
        if pending is None:
            raise ValueError("Invalid or expired OAuth state")

        # Exchange Google code for tokens
        callback_url = f"{self.server_url.rstrip('/')}/mcp/oauth/google/callback"
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

        # Extract email from ID token (Google returns a JWT)
        id_token = token_data.get("id_token")
        if not id_token:
            raise ValueError("Google did not return an ID token")

        email = extract_email_from_id_token(id_token)
        if not email:
            raise ValueError("Could not extract email from Google ID token")

        if not await self._is_user_authorized(email):
            raise ValueError(
                f"Access denied: {email} is not authorized to use this MCP Hero instance"
            )

        # Generate MCPolis authorization code
        auth_code = secrets.token_urlsafe(32)
        now = time.time()
        assert pending.client.client_id is not None
        self._auth_codes[auth_code] = StoredAuthCode(
            client_id=pending.client.client_id,
            user_email=email,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            scopes=pending.params.scopes,
            state=pending.params.state,
            created_at=now,
            expires_at=now + 300,  # 5 minutes
        )

        # Build redirect back to MCP client
        redirect_params: dict[str, str] = {"code": auth_code}
        if pending.params.state:
            redirect_params["state"] = pending.params.state

        redirect_uri = str(pending.params.redirect_uri)
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(redirect_params)}"

    # --- Token exchange ---

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> StoredAuthCode | None:
        stored = self._auth_codes.get(authorization_code)
        if stored is None:
            return None
        if stored.client_id != client.client_id:
            return None
        # Auth codes expire after 5 minutes
        if time.time() - stored.created_at > 300:
            self._auth_codes.pop(authorization_code, None)
            return None
        return stored

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: StoredAuthCode
    ) -> OAuthToken:
        # Remove the auth code (one-time use)
        for code, stored in list(self._auth_codes.items()):
            if stored is authorization_code:
                self._auth_codes.pop(code, None)
                break

        assert client.client_id is not None
        await self._ensure_loaded()
        now = int(time.time())

        # Create access token
        access_token_str = secrets.token_urlsafe(32)
        self._access_tokens[access_token_str] = StoredAccessToken(
            token=access_token_str,
            client_id=client.client_id,
            user_email=authorization_code.user_email,
            scopes=authorization_code.scopes or [],
            expires_at=now + ACCESS_TOKEN_TTL,
        )

        # Create refresh token
        refresh_token_str = secrets.token_urlsafe(32)
        self._refresh_tokens[refresh_token_str] = StoredRefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            user_email=authorization_code.user_email,
            scopes=authorization_code.scopes or [],
            created_at=time.time(),
        )

        await self._save_state()
        await self._publish_gateway_connected(authorization_code.user_email)

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token_str,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # --- Token verification ---

    async def load_access_token(self, token: str) -> AccessToken | None:
        await self._ensure_loaded()
        stored = self._access_tokens.get(token)
        if stored is None:
            return None
        if stored.expires_at < int(time.time()):
            self._access_tokens.pop(token, None)
            await self._save_state()
            return None
        # client_id is set to the user's email so the MCP SDK's
        # AuthenticatedUser.username becomes the email
        return AccessToken(
            token=token,
            client_id=stored.user_email,
            scopes=stored.scopes,
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
        if (
            REFRESH_TOKEN_TTL is not None
            and time.time() - stored.created_at > REFRESH_TOKEN_TTL
        ):
            self._refresh_tokens.pop(refresh_token, None)
            await self._save_state()
            return None
        return stored

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        await self._ensure_loaded()

        # Remove old refresh token (rotation)
        self._refresh_tokens.pop(refresh_token.token, None)

        assert client.client_id is not None
        now = int(time.time())
        effective_scopes = scopes if scopes else refresh_token.scopes

        # New access token
        access_token_str = secrets.token_urlsafe(32)
        self._access_tokens[access_token_str] = StoredAccessToken(
            token=access_token_str,
            client_id=client.client_id,
            user_email=refresh_token.user_email,
            scopes=effective_scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
        )

        # New refresh token
        new_refresh_str = secrets.token_urlsafe(32)
        self._refresh_tokens[new_refresh_str] = StoredRefreshToken(
            token=new_refresh_str,
            client_id=client.client_id,
            user_email=refresh_token.user_email,
            scopes=effective_scopes,
            created_at=time.time(),
        )

        await self._save_state()
        await self._publish_gateway_connected(refresh_token.user_email)

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=new_refresh_str,
            scope=" ".join(effective_scopes) if effective_scopes else None,
        )

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
            if stored.expires_at > now:
                emails.add(stored.user_email)
        for stored_r in self._refresh_tokens.values():
            if REFRESH_TOKEN_TTL is None or time.time() - stored_r.created_at <= REFRESH_TOKEN_TTL:
                emails.add(stored_r.user_email)
        return sorted(emails)

    def revoke_user_tokens(self, email: str) -> int:
        """Revoke all access and refresh tokens for a user (global).
        Returns count removed."""
        removed = 0
        for token_str, stored in list(self._access_tokens.items()):
            if stored.user_email == email:
                self._access_tokens.pop(token_str, None)
                removed += 1
        for token_str, stored_r in list(self._refresh_tokens.items()):
            if stored_r.user_email == email:
                self._refresh_tokens.pop(token_str, None)
                removed += 1
        if removed:
            self._schedule_save()
        return removed

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

        test_client_id = "test-mcp-client"
        if test_client_id not in self._clients:
            self._clients[test_client_id] = OAuthClientInformationFull(
                client_id=test_client_id,
                redirect_uris=[AnyUrl("http://localhost/test-callback")],
            )

        now = int(time.time())
        access_token_str = secrets.token_urlsafe(32)
        self._access_tokens[access_token_str] = StoredAccessToken(
            token=access_token_str,
            client_id=test_client_id,
            user_email=user_email,
            scopes=[],
            expires_at=now + ACCESS_TOKEN_TTL,
        )

        refresh_token_str = secrets.token_urlsafe(32)
        self._refresh_tokens[refresh_token_str] = StoredRefreshToken(
            token=refresh_token_str,
            client_id=test_client_id,
            user_email=user_email,
            scopes=[],
            created_at=time.time(),
        )

        await self._save_state()
        return access_token_str

    # --- Revocation ---

    async def revoke_token(
        self,
        token: StoredAccessToken | StoredRefreshToken,
    ) -> None:
        await self._ensure_loaded()
        if isinstance(token, StoredAccessToken):
            self._access_tokens.pop(token.token, None)
        else:
            self._refresh_tokens.pop(token.token, None)
        await self._save_state()

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
