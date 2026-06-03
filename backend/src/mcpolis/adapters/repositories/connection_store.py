from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]
    refresh_token_created_at: datetime | None = None
    # Wall-clock time of the last write (initial consent or rotation).
    # Persisted as ``updated_at`` in storage. Used by the periodic
    # refresh loop's max-age ceiling: if a stored token is older than
    # ``TOKEN_MAX_AGE_SECONDS`` we refresh proactively even when its
    # ``expires_at`` claims plenty of life — catches upstreams whose
    # actual access-TTL is shorter than declared (Mixpanel-like) and
    # gives genuine revocation a chance to surface within a known
    # window. ``None`` for legacy rows written before this field
    # existed; treat as "freshness unknown, fall back to expires_at".
    updated_at: datetime | None = None


class ConnectionStore:
    async def get_admin_token(self, org_id: str, upstream_id: str) -> OAuthToken | None:
        raise NotImplementedError

    async def put_admin_token(
        self, org_id: str, upstream_id: str, token: OAuthToken, authorized_by: str
    ) -> None:
        raise NotImplementedError

    async def get_user_token(self, org_id: str, user_id: str, upstream_id: str) -> OAuthToken | None:
        raise NotImplementedError

    async def put_user_token(self, org_id: str, user_id: str, upstream_id: str, token: OAuthToken) -> None:
        raise NotImplementedError

    async def delete_user_token(self, org_id: str, user_id: str, upstream_id: str) -> None:
        raise NotImplementedError

    async def delete_all_user_tokens(self, org_id: str, user_id: str) -> int:
        raise NotImplementedError

    async def delete_all_upstream_tokens(self, org_id: str, upstream_id: str) -> int:
        """Delete admin token + all per-user tokens for this upstream."""
        raise NotImplementedError

    async def get_all_stored_tokens(self, org_id: str) -> list[tuple[str, str]]:
        raise NotImplementedError

    async def get_connected_users(self, org_id: str, upstream_id: str) -> list[str]:
        raise NotImplementedError

    async def get_client_info(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    async def put_client_info(
        self, org_id: str, upstream_id: str, user_id: str, client_info: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    async def delete_client_info(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> None:
        raise NotImplementedError

    async def get_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        """Return the persisted RFC 8414 ``OAuthMetadata`` blob (as a
        plain JSON dict), or ``None`` if none has been stored for this
        scope. Pre-populated into ``OAuthContext.oauth_metadata`` by
        ``_build_oauth_provider`` so the SDK's refresh branch resolves
        the upstream's real ``token_endpoint`` instead of falling back
        to ``<base>/token`` (which 404s on Mixpanel-style upstreams —
        see §3.8 / §5.4)."""
        raise NotImplementedError

    async def put_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str,
        metadata: dict[str, Any],
    ) -> None:
        raise NotImplementedError

    async def delete_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        raise NotImplementedError

    async def get_connection_error(self, org_id: str, upstream_id: str) -> str | None:
        raise NotImplementedError

    async def set_connection_error(
        self,
        org_id: str,
        upstream_id: str,
        error: str,
        signature: dict[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError

    async def get_connection_error_signature(
        self, org_id: str, upstream_id: str,
    ) -> dict[str, Any] | None:
        """Return the captured ``RefreshFailureSignature`` (as a dict)
        persisted alongside the ``error:<upstream>`` row, or ``None``
        if no signature was recorded. Exposes the status/body that the
        SDK's WARNING-level ``Token refresh failed: …`` line discards.
        """
        raise NotImplementedError

    async def clear_connection_error(self, org_id: str, upstream_id: str) -> None:
        raise NotImplementedError

    async def record_refresh_failure(
        self,
        org_id: str,
        upstream_id: str,
        user_id: str,
        *,
        signature: dict[str, Any] | None = None,
    ) -> tuple[int, datetime]:
        """Increment this ``(upstream, user)``'s consecutive-failure
        counter. Sets ``first_failure_at`` if this is the first
        failure after a reset. Returns the new ``(count,
        first_failure_at)`` tuple so the caller can inspect both
        without a follow-up read.

        If ``signature`` is provided (the §5.4 capture), it is stored
        alongside the counter so §5.2's notifier can read per-user
        failure detail even for per_user_oauth upstreams where the
        per-upstream ``error:<upstream>`` row would otherwise be
        overwritten by another user's refresh.

        Counter is reset by ``reset_refresh_failures`` (on any
        successful refresh) or by the caller after a delete-and-
        re-auth decision fires. §5.1's delete-vs-retry policy reads
        these values next to the §5.4 signature.
        """
        raise NotImplementedError

    async def reset_refresh_failures(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        """Clear the counter after a success or a delete decision."""
        raise NotImplementedError

    async def get_refresh_failures(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> tuple[int, datetime] | None:
        """Return ``(count, first_failure_at)`` or ``None`` if no
        failures have been recorded for this user/upstream."""
        raise NotImplementedError

    async def get_refresh_failure_signature(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        """Return the per-user refresh-failure signature dict (see
        §5.4), or ``None`` if no failure has been recorded or the
        caller didn't pass a signature to ``record_refresh_failure``."""
        raise NotImplementedError

    async def mark_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        """Mark this user as notified about a dead refresh for this
        upstream (§5.2). Prevents sending a second email every hour
        the failure persists. Cleared by ``clear_notified`` when a
        successful refresh restores the session."""
        raise NotImplementedError

    async def was_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> bool:
        raise NotImplementedError

    async def clear_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        """Reset the "already notified" marker — called on any
        successful refresh so the next genuine failure triggers a
        fresh email."""
        raise NotImplementedError

    async def set_enabled(self, org_id: str, upstream_id: str) -> None:
        """Remove any explicit-disabled marker so the upstream falls
        back to the default-enabled state. Idempotent. Phase E
        bistate semantic — replaces the prior tristate where
        ``set_enabled`` wrote an ``enabled: True`` row and
        ``clear_enabled`` removed it; absence of a row is now the
        canonical "enabled" state."""
        raise NotImplementedError

    async def set_disabled(self, org_id: str, upstream_id: str) -> None:
        """Persist an explicit ``enabled: False`` so the boot
        reconciler skips this upstream across restarts. The only
        durable state stored — absence implies enabled."""
        raise NotImplementedError

    async def is_enabled(self, org_id: str, upstream_id: str) -> bool:
        """``True`` iff no explicit-disabled marker exists."""
        raise NotImplementedError

    async def get_disabled_ids(self, org_id: str) -> set[str]:
        raise NotImplementedError

    async def put_pending_code(
        self, org_id: str, upstream_id: str, user_id: str, code: str, original_state: str
    ) -> None:
        raise NotImplementedError

    async def pop_pending_code(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> tuple[str, str] | None:
        raise NotImplementedError

    # ── started_config_hash ──────────────────────────────────────────
    # Snapshot of ``compute_upstream_runtime_hash`` taken at the moment
    # the running session was last (re)started against this upstream's
    # config. Drives the dashboard's ``DirtyConfigBanner``: when this
    # value differs from the freshly-computed hash, the saved config
    # has drifted from what's running. Persisted (rather than
    # in-memory only) so it survives a backend restart — the bug it
    # fixes was OAuth-mode upstreams where ``ready=true`` is purely
    # token-based and no in-memory ``UpstreamState.started_config_hash``
    # ever gets written for the new process.
    async def get_started_config_hash(
        self, org_id: str, upstream_id: str,
    ) -> str | None:
        raise NotImplementedError

    async def set_started_config_hash(
        self, org_id: str, upstream_id: str, config_hash: str,
    ) -> None:
        raise NotImplementedError

    async def clear_started_config_hash(
        self, org_id: str, upstream_id: str,
    ) -> None:
        raise NotImplementedError
