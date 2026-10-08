"""Periodic OAuth token refresh for upstream MCP servers.

The app's background loop walks every stored ``(upstream, user)``
pair every ``TOKEN_REFRESH_INTERVAL`` and refreshes tokens whose access
credential expires within ``TOKEN_REFRESH_MARGIN``. The margin is
the mitigation for §3.2 (boundary-crossing at expiry) from
``internal/documents/oauth-durability.md``: by rotating in the quiet
background window, live tool-call paths never race the SDK's 401
handler into the authorization_code grant.

Split out of ``upstream_connection_service.py`` during the
post-§5.2 cleanup. The provider primitives (``RefreshFailureSignature``,
``_extract_refresh_failure``, ``_build_oauth_provider``, the
noop redirect/callback helpers, ``probe_upstream_for_auth``) still
live in the service module — this file depends on them. Tests that
monkeypatch refresh-path internals target ``httpx.AsyncClient``
itself, because the probe issues its request from the service module
rather than from here — see ``test_refresh_token_retry.py``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import structlog
from structlog.contextvars import bound_contextvars

from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
    OAuthToken,
)
from mcpolis.domain.model.oauth_errors import TERMINAL_AUTH_ERROR_CODES
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.cancel_shield import finish_despite_cancels
from mcpolis.domain.services.sign_in_refresh_lock import SignInRefreshLock
from mcpolis.domain.services.upstream_health_check import SignInWarner
from mcpolis.domain.services.upstream_connection_service import (
    SilentReconnectAuthRequired,
    failure_revision,
    tokens_are_still_stored,
    _build_oauth_provider,  # pyright: ignore[reportPrivateUsage]
    _exception_chain_contains,  # pyright: ignore[reportPrivateUsage]
    _extract_refresh_failure,  # pyright: ignore[reportPrivateUsage]
    _noop_callback,  # pyright: ignore[reportPrivateUsage]
    _noop_redirect,  # pyright: ignore[reportPrivateUsage]
    _synthesize_silent_reconnect_signature,  # pyright: ignore[reportPrivateUsage]
    delete_refused_sign_in,
    probe_upstream_for_auth,
)


logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


TOKEN_REFRESH_INTERVAL = 10 * 60  # 10 minutes
TOKEN_REFRESH_MARGIN = 20 * 60  # refresh if expiring within 20 minutes
TOKEN_REFRESH_MAX_RETRIES = 3
TOKEN_REFRESH_RETRY_DELAY = 5  # seconds
# One try's probe, the refresh in it included, at most.
TOKEN_REFRESH_ATTEMPT_TIMEOUT = 10  # seconds

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
    refresh_lock: SignInRefreshLock | None = None,
    warner: SignInWarner | None = None,
) -> None:
    """Refresh OAuth tokens for one (upstream, user) pair.

    Only attempts refresh if the access token is close to expiring.

    ``refresh_lock`` is the one-refresh-per-sign-in lock
    (``SignInRefreshLock``) that a reconnect of the same sign-in takes
    too. While another refresh of this sign-in is under way (in this
    process, or on another backend in cloud mode), this one is skipped.
    ``None``: a lock of its own, which nothing else holds.

    When ``warner`` is provided, a terminal verdict (``invalid_grant`` or
    ``invalid_client``) emails the §5.2 re-auth warning *inline*, right
    after the sign-in is deleted (``delete_refused_sign_in``). This is the
    only place the user can be reached for a refresh that dies with no
    live session: the hourly health-email sweep walks stored tokens, and
    the delete removes the row. Pass ``None`` (the default) to skip the
    email, e.g. when the health-email feature flag is off.
    """
    if upstream.http is None:
        return
    lock = refresh_lock if refresh_lock is not None else SignInRefreshLock()
    async with lock.hold_if_free(org_id, upstream.id, user_id) as held:
        if not held:
            logger.debug(
                "oauth.token.refresh.skipped.locked",
                upstream_id=upstream.id,
                user=user_id,
                org_id=org_id,
            )
            return
        await _refresh_holding_the_lock(
            org_id, upstream, user_id, connection_store, server_url,
            warner=warner,
        )


async def _refresh_holding_the_lock(
    org_id: str,
    upstream: UpstreamDefinition,
    user_id: str,
    connection_store: ConnectionStore,
    server_url: str,
    *,
    warner: SignInWarner | None,
) -> None:
    """``refresh_token_for_user``, once it holds the sign-in's refresh
    lock."""
    assert upstream.http is not None
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
        return
    if raw_token is None:
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
    # This refresh works from the row just read: what it writes back lands
    # only while that row's sign-in is stored, and what it purges on a
    # rejection only while that very row is.
    storage.start_from(raw_token)
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
            await _refresh_attempt(upstream.http.url, oauth_auth)
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
    #
    # Not when this refresh saved new tokens: the upstream just accepted
    # the refresh token, so a 401 on the probe that carried the new bearer
    # (a short burst of them, while its auth backend catches up) proves
    # nothing about the sign-in. The outcome below is then a success.
    if (
        signature is None
        and auth_flow_exc is not None
        and not storage.tokens_saved
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
        # The sign-in was deleted while this refresh ran. Nothing in the
        # refresh deletes it (the SDK only clears its in-memory copy), so
        # someone did: a Disconnect, a user removal, a reconnect's
        # cleanup. Writing the backup back used to undo that and sign the
        # user in again without their say.
        logger.warning(
            "oauth.token.refresh.tokens_gone",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
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
        storage.tokens_saved
        or refreshed_token.access_token != raw_token.access_token
    ):
        # Tokens issued with no ``expires_in`` (no ``expires_at``) are a
        # success too: they used to fall through to the failure branches,
        # which deleted the sign-in this refresh had just renewed.
        new_remaining = (
            (refreshed_token.expires_at - datetime.now(UTC)).total_seconds()
            if refreshed_token.expires_at is not None
            else None
        )
        # The stored tokens changed, but maybe not through this refresh:
        # another holder of the sign-in (a reconnect, a live session) may
        # have renewed them first, rejecting this refresh's own request.
        # The sign-in works either way; only the credit differs.
        logger.info(
            "oauth.token.refresh.success"
            if storage.tokens_saved
            else "oauth.token.refresh.refreshed_elsewhere",
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
    elif signature is not None and not await tokens_are_still_stored(
        connection_store, org_id, upstream.id, user_id,
        failure_revision(oauth_auth, storage),
    ):
        # Rejected, but the tokens it was about are no longer stored: the
        # user signed in again or disconnected, or another refresh saved
        # newer tokens, meanwhile. Record nothing, email nobody, delete
        # nothing, as ``_classify_reconnect_failure`` does.
        logger.info(
            "oauth.token.refresh.failure_of_replaced_tokens",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
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
        if signature.error_code in TERMINAL_AUTH_ERROR_CODES:
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
            # of re-presenting the dead client_id forever. The funnel then
            # emails the §5.2 warning (when ``warner`` is set), the same
            # way a reconnect that deletes a refused sign-in does.
            await delete_refused_sign_in(
                connection_store, org_id, upstream, user_id,
                revision=failure_revision(oauth_auth, storage),
                warner=warner,
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


async def _refresh_attempt(url: str, oauth_auth: httpx.Auth) -> None:
    """One try of the periodic refresh (the probe that makes the sign-in
    library refresh the tokens and save them), finished even when the
    refresh is cancelled meanwhile: the shutdown cancels the periodic
    loop, and a cancel landing after the upstream issued new tokens (and
    retired the refresh token) but before they were saved left a dead
    sign-in. The cancel then ends the refresh: no retry, and the sign-in's
    lock is let go only once the save is done. Bounded by the probe's
    timeout (``TOKEN_REFRESH_ATTEMPT_TIMEOUT``), well within what the
    shutdown waits for a loop that outlives its cancel (the job drain).

    The same shield as a reconnect's refresh (``_refresh_to_completion``),
    except that the caller waits for the try to end: it holds the
    sign-in's refresh lock across its retries."""
    await finish_despite_cancels(
        probe_upstream_for_auth(
            url, oauth_auth, timeout=TOKEN_REFRESH_ATTEMPT_TIMEOUT,
        ),
        held_by=None,
    )


async def refresh_org_sign_ins(
    org_id: str,
    upstreams: list[UpstreamDefinition],
    connection_store: ConnectionStore,
    server_url: str,
    *,
    is_member: Callable[[str], bool],
    is_stopped: Callable[[str], bool],
    refresh_lock: SignInRefreshLock | None = None,
    warner: SignInWarner | None = None,
) -> None:
    """One pass of the periodic refresh over one org: refresh each stored
    sign-in to one of its OAuth ``upstreams`` (``refresh_token_for_user``).

    A sign-in whose owner is not a member of the org (removed, or one
    that landed while they were being removed) is skipped: nobody may use
    it, and keeping it alive would end with an email about an org they
    are no longer in.

    So are the sign-ins to an MCP an admin stopped: nobody can use it
    until Start, which reconnects from the kept sign-ins (refreshing one
    that is due then), and the gateway stays off a stopped MCP meanwhile.
    A refusal while stopped used to delete a sign-in Stop promised to
    keep, with no email (the warner skips stopped MCPs).
    """
    oauth_upstreams: dict[str, UpstreamDefinition] = {}
    for upstream in upstreams:
        if upstream.auth.mode not in (
            AuthMode.admin_oauth, AuthMode.per_user_oauth,
        ):
            continue
        if is_stopped(upstream.id):
            logger.debug(
                "oauth.token.refresh.skipped.stopped",
                upstream_id=upstream.id,
                org_id=org_id,
            )
            continue
        oauth_upstreams[upstream.id] = upstream
    for upstream_id, user_id in await connection_store.get_all_stored_tokens(
        org_id,
    ):
        upstream = oauth_upstreams.get(upstream_id)
        if upstream is None:
            continue
        if not is_member(user_id):
            logger.info(
                "oauth.token.refresh.skipped.not_a_member",
                upstream_id=upstream_id,
                user=user_id,
                org_id=org_id,
            )
            continue
        # Bind per-iteration context so log lines emitted during this
        # refresh — including the MCP SDK's ``mcp.client.auth.oauth2``
        # ERROR records and any httpx output, which carry no
        # upstream/user of their own — automatically gain
        # org/upstream/user via ``foreign_pre_chain``'s
        # ``merge_contextvars``. Scoped via ``bound_contextvars`` so each
        # iteration's bindings can't leak into the next.
        with bound_contextvars(
            org_id=org_id,
            upstream_id=upstream.id,
            user_id=user_id,
        ):
            await refresh_token_for_user(
                org_id, upstream, user_id,
                connection_store, server_url,
                refresh_lock=refresh_lock,
                warner=warner,
            )
