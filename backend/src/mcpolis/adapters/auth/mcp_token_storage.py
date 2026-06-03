"""Adapter between FileConnectionStore and MCP SDK's TokenStorage protocol."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
)

from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
)
from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as InternalOAuthToken,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class McpTokenStorage:
    """Implements MCP SDK's TokenStorage protocol.

    Wraps a ConnectionStore, scoped to a specific (org_id, upstream_id, user_id).
    Converts between the SDK's OAuthToken (Pydantic) and our internal
    OAuthToken (dataclass).

    ``refresh_margin_seconds`` (optional) forces the SDK to treat a
    stored token as already-invalid when the real ``expires_at`` is
    within that many seconds of now. This is how we make our domain-
    level 20-min refresh margin actually effective: the SDK's own
    ``OAuthContext.is_token_valid()`` uses a zero buffer (a token is
    valid until the last second), so without clamping ``expires_in``
    on the way out of storage, the SDK's refresh branch only runs
    reactively at real expiry. Defaults to 0.0 (no clamping, SDK-
    native behavior) so any caller that wants the plain adapter
    semantics still gets them.

    ``max_age_seconds`` (optional) is the second clamp — forces the
    SDK to treat a stored token as invalid when its ``updated_at`` is
    older than the threshold, regardless of declared ``expires_at``.
    Implements the §3.6 stale-bearer seatbelt for upstreams whose
    actual access-TTL is shorter than declared (Mixpanel-like). Without
    this clamp, ``oauth_refresh._token_needs_refresh`` can decide
    "yes, refresh!" but the SDK's ``async_auth_flow`` then sees the
    token as valid (because expires_at is far in the future) and
    silently skips the refresh branch — a 2026-04-25 dev-env bug
    where the seatbelt fired 77 times in 13 hours without producing a
    single rotation. Defaults to ``None`` (no max-age clamp).
    """

    def __init__(
        self,
        connection_store: ConnectionStore,
        org_id: str,
        upstream_id: str,
        user_id: str,
        *,
        refresh_margin_seconds: float = 0.0,
        max_age_seconds: float | None = None,
    ) -> None:
        self._store = connection_store
        self._org_id = org_id
        self._upstream_id = upstream_id
        self._user_id = user_id
        self._refresh_margin_s = refresh_margin_seconds
        self._max_age_s = max_age_seconds

    @property
    def org_id(self) -> str:
        return self._org_id

    @property
    def upstream_id(self) -> str:
        return self._upstream_id

    @property
    def user_id(self) -> str:
        return self._user_id

    async def get_tokens(self) -> OAuthToken | None:
        """Get stored tokens (MCP SDK TokenStorage protocol)."""
        internal = await self._store.get_user_token(
            self._org_id, self._user_id, self._upstream_id
        )
        if internal is None:
            return None
        return _internal_to_sdk_token(
            internal,
            refresh_margin_seconds=self._refresh_margin_s,
            max_age_seconds=self._max_age_s,
        )

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Store tokens (MCP SDK TokenStorage protocol).

        Every successful OAuth rotation — periodic refresh loop,
        SDK-inline refresh during a live tool call, silent-reconnect
        path, initial consent flow — flows through here. Logging
        ``oauth.token.storage.rotated`` on each write gives a single
        queryable event for "a token changed," independent of the
        code path that caused it. Pair with
        ``oauth.token.refresh.started`` / ``.success`` (periodic loop)
        to distinguish periodic rotations from inline / SDK-driven
        ones: any ``storage.rotated`` without a matching ``refresh.*``
        pair within ~1s was triggered by something other than the
        periodic loop (most commonly a real tool call or the liveness
        probe's ``list_tools`` firing the SDK's refresh branch).
        """
        internal = _sdk_to_internal_token(tokens)
        previous = await self._store.get_user_token(
            self._org_id, self._user_id, self._upstream_id,
        )
        await self._store.put_user_token(
            self._org_id, self._user_id, self._upstream_id, internal,
        )
        # Last 6 chars of the access token — enough to visually confirm
        # the value actually changed (not just a re-write of the same
        # row) without logging the full credential.
        logger.info(
            "oauth.token.storage.rotated",
            upstream_id=self._upstream_id,
            user=self._user_id,
            org_id=self._org_id,
            expires_in_seconds=tokens.expires_in,
            previous_access_suffix=(
                previous.access_token[-6:] if previous is not None else None
            ),
            new_access_suffix=tokens.access_token[-6:],
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Get stored client registration info."""
        data = await self._store.get_client_info(
            self._org_id, self._upstream_id, self._user_id
        )
        if data is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(data)
        except Exception:
            logger.warning(
                "token_storage.client_info.deserialize.failed",
                upstream_id=self._upstream_id,
                user=self._user_id,
                org_id=self._org_id,
            )
            return None

    async def set_client_info(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        """Store client registration info."""
        await self._store.put_client_info(
            self._org_id,
            self._upstream_id,
            self._user_id,
            client_info.model_dump(mode="json"),
        )

    async def delete_client_info(self) -> None:
        """Drop the stored client registration so a fresh DCR runs."""
        await self._store.delete_client_info(
            self._org_id, self._upstream_id, self._user_id,
        )

    async def get_oauth_metadata(self) -> OAuthMetadata | None:
        """Return the persisted RFC 8414 ``OAuthMetadata`` for this
        upstream/user, or ``None`` if none has been stored.

        Not part of the MCP SDK's ``TokenStorage`` protocol — called by
        ``_build_oauth_provider`` to pre-populate
        ``OAuthContext.oauth_metadata`` so the SDK's refresh branch
        resolves the upstream's real ``token_endpoint`` instead of
        falling back to ``<base>/token`` (the §3.8 / §5.4 bug).
        """
        data = await self._store.get_oauth_metadata(
            self._org_id, self._upstream_id, self._user_id,
        )
        if data is None:
            return None
        try:
            return OAuthMetadata.model_validate(data)
        except Exception:
            # Schema drift (SDK upgrade adds/changes a required field)
            # or a corrupt row. Log and fall back to ``None`` so the
            # SDK's refresh branch goes back to the ``<base>/token``
            # fallback rather than crashing — the upstream just gets
            # one cycle of degraded behavior until the next 401-recovery
            # rediscovers and overwrites the stale row.
            logger.warning(
                "upstream.oauth.metadata.deserialize.failed",
                upstream_id=self._upstream_id,
                user=self._user_id,
                org_id=self._org_id,
            )
            return None

    async def set_oauth_metadata(self, metadata: OAuthMetadata) -> None:
        """Persist the upstream's authorization-server metadata so the
        SDK can reload it on the next process boot without re-running
        OAuth discovery."""
        await self._store.put_oauth_metadata(
            self._org_id,
            self._upstream_id,
            self._user_id,
            metadata.model_dump(mode="json"),
        )


def _internal_to_sdk_token(
    token: InternalOAuthToken,
    *,
    refresh_margin_seconds: float = 0.0,
    max_age_seconds: float | None = None,
) -> OAuthToken:
    """Convert internal OAuthToken to MCP SDK OAuthToken.

    The SDK's ``OAuthContext.is_token_valid()`` is the sole gate that
    decides whether ``async_auth_flow`` takes the refresh_token branch
    (vs. falling through to authorization_code on the next 401 — which
    our silent-reconnect flow blocks via ``_noop_callback``). That check
    needs a real ``token_expiry_time``, which the SDK derives from
    ``expires_in``. Passing ``None`` here makes every stored token look
    eternally valid and silently disables refresh.

    Two independent triggers clamp ``expires_in = -1`` (the smallest
    sentinel that flows through ``calculate_token_expiry`` to a past
    ``token_expiry_time``; any negative value works):

    - ``refresh_margin_seconds`` — the domain-level proactive margin.
      When the stored token's real ``expires_at`` is within the margin,
      clamp so ``is_token_valid()`` returns False and
      ``async_auth_flow`` takes the refresh_token branch **before** the
      request is sent. Eliminates the §3.2 expiry-boundary-crossing
      hazard where a request leaves the process with a "still valid"
      bearer that arrives at the upstream past expiry and triggers
      the SDK's 401 handler (which goes to ``authorization_code``
      grant, not refresh_token, and in our silent paths hits
      ``_noop_callback`` → RuntimeError → outer ``except`` wipes the
      user's tokens).
    - ``max_age_seconds`` — the §3.6 stale-bearer seatbelt. When the
      stored token's ``updated_at`` is older than this threshold **and
      the token carries a refresh_token**, clamp regardless of declared
      ``expires_at``. Targets upstreams whose actual bearer TTL is
      shorter than what they put in ``expires_in`` (Mixpanel-like).
      Without THIS clamp, the ``oauth_refresh.refresh_token_for_user``
      periodic loop could decide "refresh needed by max_age", invoke
      the trigger, and then have the SDK skip the refresh branch because
      the token still looked valid by ``expires_at`` — which is exactly
      the bug we hit in dev on 2026-04-25 (77 ``refresh.started`` events
      with 0 ``storage.rotated`` events for the same upstream).

      The refresh_token gate is load-bearing: a rotation *is* a
      refresh_token grant, so the seatbelt is meaningless without one.
      Forcing the clamp on a refresh-token-less token (e.g. a long-lived
      access token issued with no refresh_token) doesn't rotate — it
      strips the auth header, self-inflicts a 401, and our silent
      ``_noop_callback`` deletes a still-valid token every cycle.

    Both clamps default to no-op so any caller that wants the plain
    adapter semantics still gets them.
    """
    scope: str | None = None
    if token.scopes:
        scope = " ".join(token.scopes)
    expires_in: int | None = None
    now = datetime.now(UTC)
    if token.expires_at is not None:
        remaining = (token.expires_at - now).total_seconds()
        if remaining < refresh_margin_seconds:
            expires_in = -1
        else:
            expires_in = int(remaining)
    # §3.6 seatbelt: clamp on stale ``updated_at`` regardless of
    # ``expires_at``. Set last so a stale-but-not-yet-expiring token
    # still gets clamped; the margin clamp above only fires for the
    # expiry path.
    #
    # Gated on ``token.refresh_token``: the seatbelt's whole job is to
    # force a *rotation*, and a rotation is a refresh_token grant. With
    # no refresh token the SDK's ``can_refresh_token()`` is False, so
    # clamping ``expires_in = -1`` doesn't rotate anything — it strips
    # the auth header off the next request, self-inflicts a 401, drops
    # the SDK into the authorization_code grant, and our ``_noop_callback``
    # synthesizes ``invalid_grant`` → we delete a token that was still
    # valid for its full declared TTL. That's the mee6 case: a 365-day
    # access token with no refresh token, destroyed every 4h. Nothing to
    # rotate → don't force-expire; the hourly liveness probe (§4.5) still
    # detects genuine early invalidation non-destructively.
    if (
        max_age_seconds is not None
        and token.refresh_token
        and token.updated_at is not None
        and (now - token.updated_at).total_seconds() > max_age_seconds
    ):
        expires_in = -1
    return OAuthToken(
        access_token=token.access_token,
        token_type="Bearer",
        expires_in=expires_in,
        scope=scope,
        refresh_token=token.refresh_token,
    )


def _sdk_to_internal_token(token: OAuthToken) -> InternalOAuthToken:
    """Convert MCP SDK OAuthToken to internal OAuthToken."""
    expires_at = None
    if token.expires_in is not None:
        expires_at = datetime.now(UTC) + timedelta(seconds=token.expires_in)

    scopes: list[str] = []
    if token.scope:
        scopes = token.scope.split()

    return InternalOAuthToken(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        expires_at=expires_at,
        scopes=scopes,
        refresh_token_created_at=datetime.now(UTC) if token.refresh_token else None,
    )
