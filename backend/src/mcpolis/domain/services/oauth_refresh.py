"""Periodic OAuth token refresh for upstream MCP servers.

Walks every stored ``(upstream, user)`` pair every
``TOKEN_REFRESH_INTERVAL`` and refreshes tokens whose access
credential expires within ``TOKEN_REFRESH_MARGIN``. The margin is
the mitigation for §3.2 (boundary-crossing at expiry) from
``internal/documents/oauth-durability.md``: by rotating in the quiet
background window, live tool-call paths never race the SDK's 401
handler into the authorization_code grant.

Split out of ``upstream_connection_service.py`` during the
post-§5.2 cleanup. The provider primitives (``RefreshFailureSignature``,
``_extract_refresh_failure``, ``_build_oauth_provider``, the
noop redirect/callback helpers, ``_inject_accept_json``) still live
in the service module — this file depends on them. Tests that
monkeypatch refresh-path internals now target this module's
``httpx`` / ``asyncio`` rebinding — see
``test_refresh_token_retry.py``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime

import httpx
import structlog

from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
    OAuthToken,
)
from mcpolis.adapters.upstream_clients.safe_http_transport import (
    SafeAsyncHTTPTransport,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports.distributed_lock import DistributedLock
from mcpolis.domain.ports.email_sender import EmailSender
from mcpolis.domain.services.upstream_health_check import (
    AdminEmailResolver,
    check_and_notify_upstream,
)
from mcpolis.domain.services.upstream_connection_service import (
    SilentReconnectAuthRequired,
    _build_oauth_provider,  # pyright: ignore[reportPrivateUsage]
    _exception_chain_contains,  # pyright: ignore[reportPrivateUsage]
    _extract_refresh_failure,  # pyright: ignore[reportPrivateUsage]
    _inject_accept_json,  # pyright: ignore[reportPrivateUsage]
    _noop_callback,  # pyright: ignore[reportPrivateUsage]
    _noop_redirect,  # pyright: ignore[reportPrivateUsage]
    _synthesize_silent_reconnect_signature,  # pyright: ignore[reportPrivateUsage]
    _TERMINAL_AUTH_ERROR_CODES,  # pyright: ignore[reportPrivateUsage]
    purge_user_oauth_state,
)


logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


TOKEN_REFRESH_INTERVAL = 10 * 60  # 10 minutes
TOKEN_REFRESH_MARGIN = 20 * 60  # refresh if expiring within 20 minutes
TOKEN_REFRESH_MAX_RETRIES = 3
TOKEN_REFRESH_RETRY_DELAY = 5  # seconds

# Max-age ceiling for stored tokens, regardless of declared expires_at.
# Catches upstreams whose actual access-token TTL is shorter than the
# value they put in expires_in (Mixpanel-like — we lost a session on
# 2026-04-25 because it sat unrefreshed for 13h with a 24h declared
# TTL, but the upstream rejected the bearer well before then). Keeps a
# rotation cadence even for never-exercised long-TTL tokens, so any
# silent server-side invalidation surfaces within this window instead
# of the next user-facing tool call. Set per-upstream override later
# if some provider hates the extra rotation rate.
TOKEN_MAX_AGE_SECONDS = 4 * 60 * 60


def _token_needs_refresh(token: OAuthToken) -> tuple[bool, str]:
    """Decide whether the periodic loop should refresh this token.

    Returns ``(needs_refresh, reason)`` so the caller can log why.
    Two triggers:

    - **margin** — declared ``expires_at`` is within ``TOKEN_REFRESH_MARGIN``
      of now (the §3.2 boundary-crossing protection).
    - **max_age** — stored ``updated_at`` is older than
      ``TOKEN_MAX_AGE_SECONDS`` (the seatbelt against upstreams whose
      real bearer TTL is shorter than declared, or where the bearer
      was server-side invalidated for non-expiry reasons).

    Reason ``"none"`` means no refresh needed.
    """
    now = datetime.now(UTC)
    if token.expires_at is not None:
        remaining = (token.expires_at - now).total_seconds()
        if remaining < TOKEN_REFRESH_MARGIN:
            return True, "margin"
    if token.updated_at is not None:
        age = (now - token.updated_at).total_seconds()
        if age > TOKEN_MAX_AGE_SECONDS:
            return True, "max_age"
    return False, "none"


async def refresh_token_for_user(
    org_id: str,
    upstream: UpstreamDefinition,
    user_id: str,
    connection_store: ConnectionStore,
    server_url: str,
    distributed_lock: DistributedLock | None = None,
    email_sender: EmailSender | None = None,
    admin_email_resolver: AdminEmailResolver | None = None,
    hmac_key: bytes | None = None,
) -> None:
    """Refresh OAuth tokens for one (upstream, user) pair.

    Only attempts refresh if the access token is close to expiring.
    Backs up tokens before the request and restores them if they
    vanish due to a transient network error (as opposed to a genuine
    auth rejection).

    When ``distributed_lock`` is provided (cloud mode), acquires a
    per-upstream-user lock so only one backend refreshes at a time.

    When ``email_sender`` / ``admin_email_resolver`` / ``hmac_key`` are
    all provided, an ``invalid_grant`` verdict triggers the §5.2 re-auth
    notification *inline*, before the token is deleted. This is the only
    place the user can be reached for a refresh that dies with no live
    session: the hourly health-email sweep walks stored tokens, but the
    delete below removes the row (and ``reset_refresh_failures`` clears
    the signature), so a notification deferred to that sweep would never
    find this pair. Pass ``None`` (the default) to skip notification —
    e.g. when the health-email feature flag is off.
    """
    if upstream.http is None:
        return

    # Distributed lock: skip if another backend is already refreshing.
    lock_key = f"lock:token_refresh:{org_id}:{upstream.id}:{user_id}"
    if distributed_lock is not None:
        acquired = await distributed_lock.acquire(lock_key, ttl_seconds=60)
        if not acquired:
            logger.debug(
                "oauth.token.refresh.skipped.locked",
                upstream_id=upstream.id,
                user=user_id,
                org_id=org_id,
            )
            return

    # Any exception here means the stored token is unreadable — most
    # commonly a decryption failure after an encryption-key / HKDF
    # rotation. The next tool-call path will hit
    # ``reconnect_with_stored_tokens``, which surfaces
    # ``token_refresh_failed`` so the admin UI prompts for re-auth; we
    # just need to avoid crashing the periodic loop here.
    try:
        raw_token = await connection_store.get_user_token(
            org_id, user_id, upstream.id
        )
    except Exception:
        logger.exception(
            "oauth.token.refresh.read_failed",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
        )
        if distributed_lock is not None:
            await distributed_lock.release(lock_key)
        return
    if raw_token is None:
        if distributed_lock is not None:
            await distributed_lock.release(lock_key)
        return

    needs, reason = _token_needs_refresh(raw_token)
    if not needs:
        remaining = (
            (raw_token.expires_at - datetime.now(UTC)).total_seconds()
            if raw_token.expires_at else float("inf")
        )
        logger.debug(
            "oauth.token.refresh.skipped.not_needed",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
            remaining_seconds=remaining,
        )
        return

    remaining = (
        (raw_token.expires_at - datetime.now(UTC)).total_seconds()
        if raw_token.expires_at else float("inf")
    )
    age_seconds = (
        (datetime.now(UTC) - raw_token.updated_at).total_seconds()
        if raw_token.updated_at else None
    )

    # Pass BOTH clamps so the SDK actually fires its refresh branch.
    # Without ``max_age_seconds``, the periodic loop's ``reason="max_age"``
    # decision wouldn't translate into a real rotation: the SDK's
    # ``async_auth_flow`` only takes the refresh branch when
    # ``is_token_valid()`` returns False, which only happens when a
    # clamp injects ``expires_in = -1`` at the storage-read boundary.
    # The 2026-04-25 dev-env bug fired 77 ``refresh.started`` events
    # in 13 hours with 0 ``storage.rotated`` events for the same
    # upstream because we only passed the margin clamp here.
    storage = McpTokenStorage(
        connection_store, org_id, upstream.id, user_id,
        refresh_margin_seconds=TOKEN_REFRESH_MARGIN,
        max_age_seconds=TOKEN_MAX_AGE_SECONDS,
    )
    oauth_auth = await _build_oauth_provider(
        upstream, storage,
        _noop_redirect, _noop_callback,
        server_url,
    )

    # Resolve the token endpoint the SDK is about to POST to. With
    # persisted ``oauth_metadata`` it's the upstream's real endpoint;
    # without it, the SDK falls back to ``<base>/token`` (the §3.8
    # signature). Logging the URL alongside ``refresh.started`` makes
    # the smoking-gun query trivial: any 404 ``refresh.rejected`` whose
    # preceding ``refresh.started`` shows ``token_endpoint`` ending in
    # ``/token`` (vs the upstream's actual path) is §3.8 in action.
    token_endpoint: str | None = None
    if oauth_auth.context.oauth_metadata is not None:
        token_endpoint = str(
            oauth_auth.context.oauth_metadata.token_endpoint,
        )
    logger.info(
        "oauth.token.refresh.started",
        upstream_id=upstream.id,
        user=user_id,
        org_id=org_id,
        remaining_seconds=remaining,
        age_seconds=age_seconds,
        reason=reason,
        token_endpoint=token_endpoint,
    )

    # Retry loop — only retry on network errors, not auth failures
    last_error: Exception | None = None
    # Retained so the post-loop classification can inspect the exception
    # chain for ``SilentReconnectAuthRequired`` (the SDK swallows the
    # type behind ``logger.exception("OAuth flow error")`` and re-raises;
    # we need the actual object, not just the log line, to detect it).
    auth_flow_exc: Exception | None = None
    for attempt in range(1, TOKEN_REFRESH_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                auth=oauth_auth,
                event_hooks={"request": [_inject_accept_json]},
                transport=SafeAsyncHTTPTransport(),
            ) as client:
                await asyncio.wait_for(
                    client.get(upstream.http.url),
                    timeout=10,
                )
            # Request succeeded (unusual for MCP servers, but fine)
            break
        except (
            httpx.ConnectError, httpx.ConnectTimeout,
            TimeoutError, asyncio.TimeoutError, OSError,
        ) as exc:
            # Network-level failure — server unreachable, retry
            last_error = exc
            logger.warning(
                "oauth.token.refresh.network_error",
                upstream_id=upstream.id,
                user=user_id,
                org_id=org_id,
                attempt=attempt,
                max_attempts=TOKEN_REFRESH_MAX_RETRIES,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
            )
            if attempt < TOKEN_REFRESH_MAX_RETRIES:
                await asyncio.sleep(TOKEN_REFRESH_RETRY_DELAY)
            continue
        except Exception as exc:
            # Non-network error (e.g. HTTP 401 handled by OAuth middleware)
            # — don't retry, the middleware may have already acted on it
            auth_flow_exc = exc
            break

    # Check if tokens survived
    refreshed_token = await connection_store.get_user_token(
        org_id, user_id, upstream.id
    )
    # Gap A: surface the §5.4 signature in the periodic log symmetrically
    # with ``reconnect_with_stored_tokens``. Post-``410b5bd`` the SDK
    # doesn't clear storage on a failed refresh (only its in-memory
    # context), so the actual "silent failure" case lands on the
    # ``token unchanged`` branch below — not ``refreshed_token is None``.
    # Capture once; use on every branch that wants it.
    signature = _extract_refresh_failure(oauth_auth)

    # Mirror ``_classify_reconnect_failure``'s silent-reconnect handling.
    # When the SDK's 401 handler falls into the authorization_code grant,
    # our ``_noop_callback`` raises ``SilentReconnectAuthRequired`` and the
    # SDK re-raises it after logging "OAuth flow error". No refresh request
    # was ever made, so ``_extract_refresh_failure`` returns None — but the
    # SDK's behavior is itself proof the bearer is dead AND refresh wouldn't
    # help. Synthesize an ``invalid_grant`` signature so the delete shortcut
    # below fires. Without this, the token sits in storage and gets retried
    # every ``TOKEN_REFRESH_INTERVAL`` forever (one Sentry "OAuth flow error"
    # per tick), and the §5.2 notification never reaches the user — see the
    # meerbot/MCPOLIS-BACKEND-C loop (314 retries, 0 deletions).
    if (
        signature is None
        and auth_flow_exc is not None
        and _exception_chain_contains(auth_flow_exc, SilentReconnectAuthRequired)
    ):
        signature = _synthesize_silent_reconnect_signature()
        logger.info(
            "oauth.token.refresh.synthesized_invalid_grant",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
            cause=(
                "SDK fell into authorization_code grant during "
                "periodic refresh"
            ),
        )

    if refreshed_token is None and last_error is not None:
        # Tokens vanished during a network failure — the OAuth middleware
        # may have cleared them on a transient error. Restore the backup.
        logger.warning(
            "oauth.token.refresh.tokens_lost.restoring_backup",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
        )
        await connection_store.put_user_token(
            org_id, user_id, upstream.id, raw_token
        )
    elif refreshed_token is None:
        # Tokens cleared by OAuth middleware (shouldn't happen with the
        # current SDK, which only clears context — keep the branch
        # defensively for forward SDK bumps).
        if signature is not None:
            logger.warning(
                "oauth.token.refresh.rejected",
                upstream_id=upstream.id,
                user=user_id,
                org_id=org_id,
                status_code=signature.status_code,
                error_code=signature.error_code,
                body_excerpt=signature.body_excerpt,
            )
        else:
            logger.warning(
                "oauth.token.refresh.rejected.no_signature",
                upstream_id=upstream.id,
                user=user_id,
                org_id=org_id,
            )
    elif (
        refreshed_token.access_token != raw_token.access_token
        and refreshed_token.expires_at is not None
    ):
        new_remaining = (
            refreshed_token.expires_at - datetime.now(UTC)
        ).total_seconds()
        logger.info(
            "oauth.token.refresh.success",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
            expires_in_seconds=new_remaining,
        )
        # §5.1: a successful rotation resets any prior transient-failure
        # burst. Without this, a genuine recovery after 3 bad ticks
        # would still count toward the 5-strikes-out deletion threshold
        # and delete the user's token on the next unrelated blip.
        await connection_store.reset_refresh_failures(
            org_id, upstream.id, user_id,
        )
        # §5.2: a success also clears the "already emailed you" marker
        # so the next genuine failure cycle triggers a fresh
        # notification rather than staying silent.
        await connection_store.clear_notified(
            org_id, upstream.id, user_id,
        )
    elif signature is not None:
        # The silent-failure case that Gap A was specifically about:
        # refresh was attempted, upstream said no, but the SDK left
        # storage untouched. Without this log, an operator reading
        # the periodic loop sees only ``token unchanged`` at DEBUG and
        # has to wait for the next reconnect to learn that refresh is
        # broken. Also record per-user failure state so §5.1's policy
        # and §5.2's email pipeline fire without waiting for reconnect.
        logger.warning(
            "oauth.token.refresh.silent_rejection",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
            status_code=signature.status_code,
            error_code=signature.error_code,
            body_excerpt=signature.body_excerpt,
        )
        await connection_store.record_refresh_failure(
            org_id, upstream.id, user_id,
            signature=signature.to_dict(),
        )
        # §5.1: apply the delete-on-``invalid_grant`` shortcut here too.
        # Without this, a token whose refresh permanently fails (revoked,
        # client_id mismatch after a callback-URL change, …) sits in
        # storage and gets retried every ``TOKEN_REFRESH_INTERVAL`` —
        # one ERROR-level ``OAuth flow error`` from ``mcp.client.auth.oauth2``
        # per tick (the SDK's own logger, NOT a wrapped record), which
        # surfaces as a Sentry event because the SDK caught the
        # ``SilentReconnectAuthRequired`` raised by our ``_noop_callback``.
        # The reconnect path's ``_classify_reconnect_failure`` already
        # deletes on this exact signal; mirror it here so loops with no
        # active session converge instead of looping forever.
        if signature.error_code in _TERMINAL_AUTH_ERROR_CODES:
            # §5.2: notify the user *before* the purge below tears down
            # the token row + signature the notifier reads. Reuse the
            # shared notifier so the decide / mark-notified logic lives
            # in one place (DRY with the hourly health-email sweep).
            # The outer guard is belt-and-suspenders: per-recipient
            # *send* failures are already swallowed inside
            # ``check_and_notify_upstream``, so this only catches the
            # rarer resolver / store-read failures — and even then it
            # logs and proceeds to delete, because converging the loop
            # wins over a guaranteed notification (a flaky lookup must
            # not strand the doomed token and keep the loop spinning).
            if (
                email_sender is not None
                and admin_email_resolver is not None
                and hmac_key is not None
            ):
                try:
                    await check_and_notify_upstream(
                        org_id=org_id,
                        upstream=upstream,
                        user_id=user_id,
                        connection_store=connection_store,
                        email_sender=email_sender,
                        admin_email_resolver=admin_email_resolver,
                        server_url=server_url,
                        hmac_key=hmac_key,
                    )
                except Exception:
                    logger.exception(
                        "oauth.token.refresh.notify_failed",
                        upstream_id=upstream.id,
                        user=user_id,
                        org_id=org_id,
                    )
            logger.info(
                "oauth.token.refresh.tokens_deleted",
                upstream_id=upstream.id,
                user=user_id,
                org_id=org_id,
                reason=signature.error_code,
            )
            # Purge the whole per-user state. For ``invalid_client`` the
            # DCR client_info is dead too; dropping it (safe now the token
            # is gone) is what lets the next consent re-register instead
            # of re-presenting the dead client_id forever.
            await purge_user_oauth_state(
                connection_store, org_id, upstream.id, user_id,
            )
    else:
        # Raised from DEBUG to INFO so the periodic loop's "no-op tick"
        # outcome is queryable in Elastic. The MCPOLIS-BACKEND-C
        # investigation had to *infer* this branch from absence-of-
        # warning because DEBUG isn't forwarded; an INFO line here makes
        # the "started without follow-up" pattern impossible to miss
        # next time. (Post-Fix-#2 this branch is reached only when the
        # GET succeeded without triggering a refresh AND no signature
        # AND no SilentReconnect marker — rare and worth surfacing.)
        logger.info(
            "oauth.token.refresh.unchanged",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
        )

    # Release distributed lock after refresh completes.
    if distributed_lock is not None:
        await distributed_lock.release(lock_key)


async def periodic_token_refresh(
    org_id: str,
    upstreams: list[UpstreamDefinition],
    connection_store: ConnectionStore,
    server_url: str,
) -> None:
    """Background loop that refreshes OAuth tokens close to expiry.

    Runs every TOKEN_REFRESH_INTERVAL seconds. For each stored token
    that expires within TOKEN_REFRESH_MARGIN, triggers a lightweight
    HTTP request through the OAuth middleware to refresh it.
    """
    from mcpolis.domain.model.policy import AuthMode

    upstream_by_id = {u.id: u for u in upstreams}
    oauth_upstream_ids = {
        u.id for u in upstreams
        if u.auth.mode in (AuthMode.admin_oauth, AuthMode.per_user_oauth)
    }

    while True:
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
        try:
            all_tokens = await connection_store.get_all_stored_tokens(org_id)
            tasks: list[Coroutine[object, object, None]] = []
            for upstream_id, user_id in all_tokens:
                if upstream_id not in oauth_upstream_ids:
                    continue
                upstream = upstream_by_id.get(upstream_id)
                if upstream is None:
                    continue
                tasks.append(
                    refresh_token_for_user(
                        org_id, upstream, user_id, connection_store, server_url,
                    )
                )
            if tasks:
                logger.info(
                    "oauth.token.refresh.periodic.started",
                    org_id=org_id,
                    session_count=len(tasks),
                )
                await asyncio.gather(*tasks)
        except Exception:
            logger.exception(
                "oauth.token.refresh.periodic.failed",
                org_id=org_id,
            )
