"""Tests for the network retry + token-backup restore in ``refresh_token_for_user``.

Pins two behaviors the periodic refresh loop relies on to survive
transient upstream outages:

1. **Retry loop**: network errors retry up to ``TOKEN_REFRESH_MAX_RETRIES``
   times with ``TOKEN_REFRESH_RETRY_DELAY`` between attempts. Non-network
   errors break immediately (the OAuth middleware may already have acted
   on them, retrying would be redundant work or — worse — a double-401
   that consumes a single-use refresh token twice).

2. **Token backup/restore**: if the SDK's OAuth middleware clears stored
   tokens during a refresh attempt that turned out to be a transient
   network failure, we restore the pre-attempt token so the next refresh
   cycle gets another chance. On a genuine auth rejection (tokens gone
   AND no network error was seen), we leave the store empty so the user
   re-authenticates cleanly.

These are easy to break and hard to spot from logs — test them
explicitly rather than relying on prod behavior to surface regressions.
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog
from pydantic import AnyUrl

from mcp.shared.auth import OAuthClientInformationFull

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services import oauth_refresh
from mcpolis.domain.services.oauth_refresh import (
    TOKEN_REFRESH_MAX_RETRIES,
    TOKEN_REFRESH_RETRY_DELAY,
    refresh_token_for_user,
)

UPSTREAM_ID = "notion"
USER_ID = "__admin__"
UPSTREAM_URL = "https://mcp.example.invalid/mcp"
SERVER_URL = "https://gateway.example.invalid"
CALLBACK_URL = f"{SERVER_URL}/api/oauth/upstream/callback"


def _make_upstream() -> UpstreamDefinition:
    return UpstreamDefinition(
        id=UPSTREAM_ID,
        display_name="Notion",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=UPSTREAM_URL),
        auth=UpstreamAuthConfig(mode=AuthMode.admin_oauth),
    )


def _make_expiring_token() -> OAuthToken:
    """Token 5 min from expiry — inside the 20-min refresh margin."""
    return OAuthToken(
        access_token="old-at",
        refresh_token="old-rt",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )


async def _seed(store: FileConnectionStore) -> OAuthToken:
    token = _make_expiring_token()
    await store.put_user_token(DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID, token)
    await store.put_client_info(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
        OAuthClientInformationFull(
            client_id="cid",
            client_secret="csec",
            redirect_uris=[AnyUrl(CALLBACK_URL)],
            token_endpoint_auth_method="client_secret_post",
        ).model_dump(mode="json"),
    )
    return token


class _FakeAsyncClient:
    """Context-manager stand-in for ``httpx.AsyncClient``.

    All instances created for a given test share one ``behaviors``
    queue, so a behavior list like
    ``[ConnectError, ConnectError, None]`` drives three successive
    ``get()`` calls across however many ``AsyncClient(...)``
    instantiations the retry loop makes (one per attempt). Each
    behavior may be an exception instance (raised), a callable (called
    per get), or None (treated as a successful no-op response)."""

    def __init__(
        self,
        behaviors: list[Any],
        **_kwargs: Any,
    ) -> None:
        self._behaviors = behaviors  # shared list, consumed across instances
        self.get_calls = 0

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        pass

    async def get(self, _url: str) -> Any:
        self.get_calls += 1
        if not self._behaviors:
            return MagicMock(status_code=200)
        behavior = self._behaviors.pop(0)
        if callable(behavior):
            result: object = behavior()
            # Await coroutines; sync callables either already raised
            # (propagated naturally) or returned a plain value.
            if inspect.iscoroutine(result):
                return await result
            return result
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    get_behaviors: list[Any],
) -> list[_FakeAsyncClient]:
    """Patch ``httpx.AsyncClient`` used by the service module.

    The behavior list is shared across every instance created during
    the test: entry N drives the N-th ``get()`` call overall, no
    matter which ``AsyncClient(...)`` instance it lands on. Returns
    the list of instantiated clients so tests can also assert on
    instantiation counts if needed.
    """
    shared_behaviors = list(get_behaviors)
    clients: list[_FakeAsyncClient] = []

    def _factory(**kwargs: Any) -> _FakeAsyncClient:
        client = _FakeAsyncClient(shared_behaviors, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(
        "mcpolis.domain.services.oauth_refresh.httpx.AsyncClient",
        _factory,
    )
    return clients


def _install_fake_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> list[float]:
    """Record every ``asyncio.sleep`` call from the module and return
    without actually sleeping."""
    recorded: list[float] = []

    async def _sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(
        "mcpolis.domain.services.oauth_refresh.asyncio.sleep",
        _sleep,
    )
    return recorded


# ── Retry loop: network errors ───────────────────────────────────────


@pytest.mark.asyncio
async def test_network_error_retries_max_times_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three consecutive ``httpx.ConnectError``s must trigger exactly
    ``TOKEN_REFRESH_MAX_RETRIES`` attempts and ``N-1`` sleep calls of
    ``TOKEN_REFRESH_RETRY_DELAY`` seconds each. The final attempt
    doesn't sleep — the loop exits immediately."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)

    clients = _install_fake_client(
        monkeypatch,
        [httpx.ConnectError("sim")] * TOKEN_REFRESH_MAX_RETRIES,
    )
    slept = _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    total_gets = sum(c.get_calls for c in clients)
    assert total_gets == TOKEN_REFRESH_MAX_RETRIES
    assert slept == [TOKEN_REFRESH_RETRY_DELAY] * (
        TOKEN_REFRESH_MAX_RETRIES - 1
    )


@pytest.mark.asyncio
async def test_network_error_recovers_if_later_attempt_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that eventually succeeds must break out of the loop
    immediately — no further sleeps or GETs after the success."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)

    # Fail twice, then succeed.
    clients = _install_fake_client(
        monkeypatch,
        [httpx.ConnectError("sim"), httpx.ConnectError("sim"), None],
    )
    slept = _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    total_gets = sum(c.get_calls for c in clients)
    assert total_gets == 3
    # Two failures → two sleeps before the success exits the loop.
    assert slept == [TOKEN_REFRESH_RETRY_DELAY] * 2


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("sim"),
        httpx.ConnectTimeout("sim"),
        TimeoutError(),
        OSError("sim"),
    ],
)
@pytest.mark.asyncio
async def test_each_network_error_type_triggers_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
) -> None:
    """Each exception type in the network-error tuple must be treated
    uniformly — the retry branch is keyed on that tuple, so a typo
    would silently fall through to the non-network ``break``."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)

    clients = _install_fake_client(monkeypatch, [exc, None])
    slept = _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    total_gets = sum(c.get_calls for c in clients)
    assert total_gets == 2
    assert slept == [TOKEN_REFRESH_RETRY_DELAY]


# ── Retry loop: non-network errors ───────────────────────────────────


@pytest.mark.asyncio
async def test_non_network_error_breaks_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic ``Exception`` (e.g. the ``_noop_callback`` RuntimeError
    the SDK raises when a 401 handler reaches the auth_code grant) must
    NOT be retried. Retrying would either re-consume a single-use
    refresh token or spam the upstream with doomed requests."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)

    clients = _install_fake_client(
        monkeypatch,
        [RuntimeError("oauth flow error"), None],  # 2nd entry never reached
    )
    slept = _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    total_gets = sum(c.get_calls for c in clients)
    assert total_gets == 1
    assert slept == []


# ── Token backup/restore semantics ───────────────────────────────────


@pytest.mark.asyncio
async def test_tokens_restored_when_vanished_after_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the OAuth middleware clears stored tokens mid-flight AND the
    failure was a network error, restore from backup. Otherwise a
    transient DNS blip would force every user to re-authenticate."""
    store = FileConnectionStore(tmp_path)
    seeded = await _seed(store)

    async def _wipe_then_raise() -> None:
        await store.delete_user_token(
            DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
        )
        raise httpx.ConnectError("sim")

    _install_fake_client(
        monkeypatch,
        [_wipe_then_raise] * TOKEN_REFRESH_MAX_RETRIES,
    )
    _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    restored = await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    )
    assert restored is not None
    assert restored.access_token == seeded.access_token
    assert restored.refresh_token == seeded.refresh_token


@pytest.mark.asyncio
async def test_tokens_not_restored_when_cleared_by_non_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If tokens are missing but we never saw a network error, treat it
    as a genuine auth rejection — leave the store empty so the user is
    prompted to re-authenticate. Restoring here would mask real token
    revocation and leave stale state around forever."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)

    async def _wipe_then_raise_auth() -> None:
        await store.delete_user_token(
            DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
        )
        raise RuntimeError("auth rejected")

    _install_fake_client(monkeypatch, [_wipe_then_raise_auth])
    _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None


@pytest.mark.asyncio
async def test_tokens_left_alone_on_refresh_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: the GET succeeds (or the middleware refreshed
    tokens and kept them in place). Whatever the store now holds must
    not be clobbered by the backup-restore logic."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)

    async def _simulate_successful_refresh() -> None:
        # Stand in for the SDK middleware persisting a rotated token.
        await store.put_user_token(
            DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
            OAuthToken(
                access_token="fresh-at",
                refresh_token="fresh-rt",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=[],
                refresh_token_created_at=datetime.now(UTC),
            ),
        )

    _install_fake_client(
        monkeypatch, [_simulate_successful_refresh],
    )
    _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    current = await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    )
    assert current is not None
    # The fresh token the "middleware" wrote must survive — the
    # backup-restore branch must only fire when tokens are None.
    assert current.access_token == "fresh-at"
    assert current.refresh_token == "fresh-rt"


# ── Pre-flight skip paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_when_token_outside_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token more than ``TOKEN_REFRESH_MARGIN`` from expiry must not
    trigger any httpx activity. This is the hot path — firing the
    refresh loop here would generate load on every upstream every 10
    minutes."""
    store = FileConnectionStore(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
        OAuthToken(
            access_token="plenty-of-time",
            refresh_token="rt",
            expires_at=datetime.now(UTC) + timedelta(hours=5),
            scopes=[],
            refresh_token_created_at=datetime.now(UTC),
        ),
    )
    clients = _install_fake_client(monkeypatch, [])
    slept = _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    # No httpx client instantiated, no sleeps, no token mutation.
    assert clients == []
    assert slept == []


@pytest.mark.asyncio
async def test_skip_when_no_stored_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user has no stored token (e.g. tokens were wiped by the
    ``reconnect_with_stored_tokens`` outer ``except`` on a prior
    failure), refresh must no-op — not walk through the retry loop
    against an empty token."""
    store = FileConnectionStore(tmp_path)
    clients = _install_fake_client(monkeypatch, [])
    slept = _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    assert clients == []
    assert slept == []


# ── Gap A: silent-failure forensics in refresh_token_for_user ────────


@pytest.mark.asyncio
async def test_silent_refresh_failure_records_signature_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before Gap A: a refresh that failed but left storage tokens
    unchanged (the actual SDK behavior — ``_handle_refresh_response``
    only clears context on non-200) landed on the ``token unchanged``
    DEBUG branch. An admin reading the periodic log could only infer
    a break from silence, with no status / error_code / body.

    After Gap A: the same path logs at WARNING with the §5.4 signature,
    and records the per-user failure row so §5.1's policy + §5.2's
    notifier fire without waiting for the next reconnect attempt.
    Drives the real ``_InitializingOAuthClientProvider`` via a
    monkeypatched ``_build_oauth_provider`` that pre-populates
    ``last_refresh_failure`` — same shape the real SDK path produces.
    """
    from mcp.client.auth import OAuthClientProvider

    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        RefreshFailureSignature,
        _InitializingOAuthClientProvider,
        _build_oauth_provider,
    )

    store = FileConnectionStore(tmp_path)
    await _seed(store)

    # HTTP GET returns benignly — the refresh middleware has already
    # "failed" (we simulate that below by seeding the signature on the
    # provider), so the call just looks like a no-op.
    _install_fake_client(monkeypatch, [None])
    _install_fake_sleep(monkeypatch)

    real_build = _build_oauth_provider
    now = datetime.now(UTC)

    async def _build_with_sig(*args: Any, **kwargs: Any) -> OAuthClientProvider:
        provider = await real_build(*args, **kwargs)
        if isinstance(provider, _InitializingOAuthClientProvider):
            # Use a non-``invalid_grant`` OAuth error so the silent_rejection
            # branch records the signature without short-circuiting into
            # the §5.1 delete path. The delete path is covered by
            # ``test_invalid_grant_silent_rejection_deletes_token``.
            provider.last_refresh_failure = RefreshFailureSignature(
                status_code=400,
                body_excerpt='{"error":"invalid_request"}',
                error_code="invalid_request",
                timestamp=now,
            )
        return provider

    monkeypatch.setattr(
        oauth_refresh,
        "_build_oauth_provider", _build_with_sig,
    )

    with structlog.testing.capture_logs() as logs:
        await refresh_token_for_user(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            user_id=USER_ID,
            connection_store=store,
            server_url=SERVER_URL,
        )

    # Gap A event carries the captured refresh-failure signature as
    # top-level structured fields so log-backend alerts can key on
    # ``error_code`` directly.
    matching = [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.silent_rejection"
        and e.get("status_code") == 400
        and e.get("error_code") == "invalid_request"
    ]
    assert matching, f"expected Gap A event, got: {logs}"

    # Per-user failure row recorded with the signature so §5.1 + §5.2
    # can key on it without waiting for the next reconnect.
    failures = await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    )
    assert failures is not None
    count, _first_at = failures
    assert count == 1

    sig = await store.get_refresh_failure_signature(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    )
    assert sig is not None
    assert sig["error_code"] == "invalid_request"
    assert sig["status_code"] == 400

    # Token MUST stay in storage for non-``invalid_grant`` errors —
    # only the §5.1 transient-threshold path (5 strikes / 30 min)
    # deletes from the periodic loop.
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is not None


@pytest.mark.asyncio
async def test_invalid_grant_silent_rejection_deletes_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Periodic-refresh §5.1 shortcut: an upstream returning
    ``invalid_grant`` on the refresh request is a definitive verdict
    on the credential. Without this branch, the periodic loop would
    retry the dead token every ``TOKEN_REFRESH_INTERVAL`` and the SDK's
    ``mcp.client.auth.oauth2`` logger would log ERROR-level
    ``OAuth flow error`` entries (``SilentReconnectAuthRequired``
    traceback) on every tick — one Sentry event per stuck token per
    10 minutes. Pinned by the 2026-05-07 mcpolis.seniak.com →
    mcphero.io rebrand incident. Mirrors the deletion path in
    ``_classify_reconnect_failure`` so loops with no live session
    converge instead of spinning forever.
    """
    from mcp.client.auth import OAuthClientProvider

    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        RefreshFailureSignature,
        _InitializingOAuthClientProvider,
        _build_oauth_provider,
    )

    store = FileConnectionStore(tmp_path)
    await _seed(store)

    _install_fake_client(monkeypatch, [None])
    _install_fake_sleep(monkeypatch)

    real_build = _build_oauth_provider
    now = datetime.now(UTC)

    async def _build_with_sig(*args: Any, **kwargs: Any) -> OAuthClientProvider:
        provider = await real_build(*args, **kwargs)
        if isinstance(provider, _InitializingOAuthClientProvider):
            provider.last_refresh_failure = RefreshFailureSignature(
                status_code=400,
                body_excerpt=(
                    '{"error":"invalid_grant",'
                    '"error_description":"Client ID mismatch"}'
                ),
                error_code="invalid_grant",
                timestamp=now,
            )
        return provider

    monkeypatch.setattr(
        oauth_refresh,
        "_build_oauth_provider", _build_with_sig,
    )

    with structlog.testing.capture_logs() as logs:
        await refresh_token_for_user(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            user_id=USER_ID,
            connection_store=store,
            server_url=SERVER_URL,
        )

    # Token deleted on the spot — no waiting for the 5-strike threshold.
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None

    # Counter reset so a re-Connect that lands fresh tokens starts
    # from zero rather than carrying the burst into the next cycle.
    assert await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is None

    # Operator-visible audit trail: silent_rejection (the symptom)
    # AND tokens_deleted (the action) both fire.
    rejection = [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.silent_rejection"
        and e.get("error_code") == "invalid_grant"
    ]
    assert rejection, f"expected silent_rejection log, got: {logs}"
    deletion = [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.tokens_deleted"
        and e.get("reason") == "invalid_grant"
    ]
    assert deletion, f"expected tokens_deleted log, got: {logs}"


# ── TOKEN_MAX_AGE_SECONDS ceiling (Fix #1) ───────────────────────────


def _token_at_age(age_seconds: float) -> OAuthToken:
    """Build a token with a far-future ``expires_at`` (well outside
    the 20-min margin) but a stale ``updated_at``. Exercises the
    max-age branch of ``_token_needs_refresh`` in isolation from the
    margin check."""
    return OAuthToken(
        access_token="fresh-looking-at",
        refresh_token="rt",
        # 12h in the future — comfortably outside the 20-min margin.
        expires_at=datetime.now(UTC) + timedelta(hours=12),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


def test_token_needs_refresh_returns_max_age_for_stale_updated_at() -> None:
    """A token with plenty of declared TTL but ``updated_at`` older
    than ``TOKEN_MAX_AGE_SECONDS`` must still trigger refresh — that's
    the seatbelt against upstreams whose actual bearer TTL is shorter
    than declared (Mixpanel-like). Reason field reports ``max_age``
    so logs distinguish the trigger from the margin path."""
    from mcpolis.domain.services.oauth_refresh import (
        TOKEN_MAX_AGE_SECONDS,
        _token_needs_refresh,  # pyright: ignore[reportPrivateUsage]
    )
    needs, reason = _token_needs_refresh(
        _token_at_age(TOKEN_MAX_AGE_SECONDS + 60),
    )
    assert needs is True
    assert reason == "max_age"


def test_token_needs_refresh_skips_when_fresh_and_outside_margin() -> None:
    """The hot path — a freshly-rotated token with plenty of TTL
    must NOT trigger refresh. If this flips, every periodic tick
    hammers every stored token regardless of need."""
    from mcpolis.domain.services.oauth_refresh import (
        _token_needs_refresh,  # pyright: ignore[reportPrivateUsage]
    )
    # Just-rotated, plenty of TTL.
    needs, reason = _token_needs_refresh(_token_at_age(60))
    assert needs is False
    assert reason == "none"


def test_token_needs_refresh_prefers_margin_over_max_age() -> None:
    """When BOTH triggers fire (token nearly expired AND old), the
    ``margin`` reason wins in the log. Pinned so an alert keyed on
    reason buckets doesn't get confused — margin is the urgent one,
    max_age is the seatbelt."""
    from mcpolis.domain.services.oauth_refresh import (
        TOKEN_MAX_AGE_SECONDS,
        _token_needs_refresh,  # pyright: ignore[reportPrivateUsage]
    )
    # Inside margin AND stale.
    token = OAuthToken(
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC) - timedelta(
            seconds=TOKEN_MAX_AGE_SECONDS + 60,
        ),
    )
    needs, reason = _token_needs_refresh(token)
    assert needs is True
    assert reason == "margin"


def test_token_needs_refresh_handles_legacy_row_without_updated_at() -> None:
    """Tokens stored before the ``updated_at`` field existed deserialize
    with ``updated_at=None``. Must not crash; max_age branch silently
    skips them. The next rotation populates ``updated_at`` and the
    seatbelt activates."""
    from mcpolis.domain.services.oauth_refresh import (
        _token_needs_refresh,  # pyright: ignore[reportPrivateUsage]
    )
    legacy = OAuthToken(
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(UTC) + timedelta(hours=12),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
        updated_at=None,
    )
    needs, reason = _token_needs_refresh(legacy)
    assert needs is False
    assert reason == "none"


@pytest.mark.asyncio
async def test_max_age_triggers_refresh_in_periodic_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end periodic-loop test for the §3.6 seatbelt.

    Asserts FOUR things, in order of importance for catching the
    2026-04-25 dev-env bug (77 ``refresh.started`` events with 0
    ``storage.rotated`` events for the same upstream):

    1. ``oauth.token.refresh.started`` fires with ``reason="max_age"``.
    2. The SDK's ``async_auth_flow`` actually issues a refresh
       request — verified by the ``_FakeAsyncClient`` recording a
       POST that flows through the auth callback (this is what
       earlier versions of this test silently skipped, by stubbing
       ``client.get(...)`` with a bare ``return None``).
    3. ``oauth.token.storage.rotated`` fires — the SDK called
       ``set_tokens(...)`` with a new bearer.
    4. ``updated_at`` on the stored token is now within the last
       few seconds (the rotation actually persisted the freshness
       stamp, closing the loop so the next tick sees the token as
       fresh and skips correctly).

    The earlier version of this test only asserted (1) and would
    have silently passed for the broken seatbelt — it monkey-patched
    ``httpx.AsyncClient`` with a stub whose ``get()`` returned
    ``None``, so the SDK's auth flow never ran. That's the exact
    blind spot that let the dev-env bug ship.
    """
    from mcpolis.domain.services.oauth_refresh import TOKEN_MAX_AGE_SECONDS

    store = FileConnectionStore(tmp_path)
    # Seed a token with future expiry but stale updated_at.
    await store.put_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
        OAuthToken(
            access_token="stale-at",
            refresh_token="stale-rt",
            expires_at=datetime.now(UTC) + timedelta(hours=12),
            scopes=[],
            refresh_token_created_at=datetime.now(UTC),
        ),
    )
    import json as _json
    raw = _json.loads(store._path.read_text())  # pyright: ignore[reportPrivateUsage]
    key = f"user:{UPSTREAM_ID}:{USER_ID}"
    raw[key]["updated_at"] = (
        datetime.now(UTC) - timedelta(
            seconds=TOKEN_MAX_AGE_SECONDS + 60,
        )
    ).isoformat()
    store._path.write_text(_json.dumps(raw, indent=2))  # pyright: ignore[reportPrivateUsage]
    await store.put_client_info(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
        OAuthClientInformationFull(
            client_id="cid",
            client_secret="csec",
            redirect_uris=[AnyUrl(CALLBACK_URL)],
            token_endpoint_auth_method="client_secret_post",
        ).model_dump(mode="json"),
    )

    # Drive a real ``httpx.AsyncClient`` against the upstream URL —
    # but intercept the network with a transport that:
    #   * routes the upstream URL to a 401 (forces SDK refresh path),
    #   * routes the token endpoint to a 200 with a fresh access token.
    # This exercises the SDK's actual ``async_auth_flow`` (refresh
    # branch + ``_handle_refresh_response`` + ``storage.set_tokens``)
    # rather than stubbing ``client.get(...)`` with a bare value
    # (which is what the prior version of this test did, silently
    # skipping the entire SDK auth flow).
    new_token_payload = {
        "access_token": "fresh-rotated-at",
        "token_type": "Bearer",
        "expires_in": 7200,
        "refresh_token": "fresh-rotated-rt",
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        # The SDK's ``_get_token_endpoint`` derives the token URL
        # from the upstream's base URL when no OAuth metadata is
        # discovered. For the upstream URL itself, return 200 so the
        # original request "succeeds" after the refresh (we don't
        # care about the body — we only assert the rotation occurred).
        if request.method == "POST" and request.url.path.endswith("/token"):
            return httpx.Response(200, json=new_token_payload)
        # Anything else: a benign 200 so the auth flow exits cleanly.
        return httpx.Response(200, text="")

    transport = httpx.MockTransport(_handler)

    real_async_client = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    monkeypatch.setattr(
        "mcpolis.domain.services.oauth_refresh.httpx.AsyncClient",
        _factory,
    )
    _install_fake_sleep(monkeypatch)

    before_updated_at_iso = raw[key]["updated_at"]

    with structlog.testing.capture_logs() as logs:
        await refresh_token_for_user(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            user_id=USER_ID,
            connection_store=store,
            server_url=SERVER_URL,
        )

    # (1) ``refresh.started`` with the right ``reason``.
    started = [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.started"
    ]
    assert len(started) == 1, (
        f"expected one refresh-started event, got: {logs}"
    )
    assert started[0].get("reason") == "max_age"
    assert started[0].get("age_seconds") is not None

    # (2) + (3) The rotation actually happened: ``storage.rotated``
    # fired, and the new access-token suffix matches what we returned
    # from the mock token endpoint.
    rotated = [
        e for e in logs
        if e.get("event") == "oauth.token.storage.rotated"
    ]
    assert len(rotated) == 1, (
        "expected one storage.rotated event — its absence is the "
        f"smoking gun the seatbelt was broken. logs: {logs}"
    )
    assert rotated[0].get("new_access_suffix") == "ted-at"  # last 6 of "fresh-rotated-at"

    # (4) ``updated_at`` on disk is fresh — the next tick will see
    # the token as fresh and skip silently. Closes the bug-loop.
    raw_after = _json.loads(store._path.read_text())  # pyright: ignore[reportPrivateUsage]
    after_updated_at_iso = raw_after[key]["updated_at"]
    assert after_updated_at_iso != before_updated_at_iso, (
        "updated_at unchanged — the rotation didn't refresh freshness "
        "stamp, so the next tick will trigger the same dead refresh"
    )
    after_updated_at = datetime.fromisoformat(after_updated_at_iso)
    age_after = (datetime.now(UTC) - after_updated_at).total_seconds()
    assert age_after < 60, (
        f"updated_at is {age_after:.0f}s old after refresh — expected <60s"
    )


# ── SilentReconnectAuthRequired (Fix #2) ─────────────────────────────


def test_synthesized_signature_has_invalid_grant_error_code() -> None:
    """The synthesizer produces a signature that §5.1's policy will
    treat as ``invalid_grant`` (delete immediately) and §5.2's
    decide_notification will treat as worth emailing about."""
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        _synthesize_silent_reconnect_signature,
    )
    sig = _synthesize_silent_reconnect_signature()
    assert sig.error_code == "invalid_grant"
    assert sig.status_code == 0  # synthesized, no real HTTP response
    assert "authorization_code grant" in sig.body_excerpt


def test_exception_chain_contains_finds_marker_in_exception_group() -> None:
    """The MCP SDK wraps failures in ``ExceptionGroup`` via anyio's
    task groups. A naive ``isinstance`` on the top-level exception
    misses the actual cause buried inside. ``_exception_chain_contains``
    walks ``.exceptions``, ``__cause__``, and ``__context__`` so the
    marker is detected wherever it lands."""
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        SilentReconnectAuthRequired,
        _exception_chain_contains,
    )

    inner = SilentReconnectAuthRequired("inner")
    wrapped_eg = BaseExceptionGroup("group", [inner])
    assert _exception_chain_contains(
        wrapped_eg, SilentReconnectAuthRequired,
    ) is True

    # Nested ExceptionGroup
    deeply = BaseExceptionGroup("outer", [
        BaseExceptionGroup("inner-group", [
            SilentReconnectAuthRequired("nested"),
        ]),
    ])
    assert _exception_chain_contains(
        deeply, SilentReconnectAuthRequired,
    ) is True

    # Negative case: unrelated exception chain
    plain = RuntimeError("nothing to do with auth")
    assert _exception_chain_contains(
        plain, SilentReconnectAuthRequired,
    ) is False


def test_exception_chain_contains_walks_cause_and_context() -> None:
    """Re-raised exceptions chain via ``__cause__`` (explicit
    ``raise ... from ...``) or ``__context__`` (implicit during
    handling). Both should be walked so the marker survives a
    re-raise wrapping."""
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        SilentReconnectAuthRequired,
        _exception_chain_contains,
    )

    # __cause__: raise ... from
    try:
        try:
            raise SilentReconnectAuthRequired("root")
        except SilentReconnectAuthRequired as src:
            raise ValueError("wrapped") from src
    except ValueError as wrapped:
        assert _exception_chain_contains(
            wrapped, SilentReconnectAuthRequired,
        ) is True

    # __context__: implicit during handling
    try:
        try:
            raise SilentReconnectAuthRequired("root")
        except SilentReconnectAuthRequired:
            raise ValueError("re-raised")
    except ValueError as wrapped:
        assert _exception_chain_contains(
            wrapped, SilentReconnectAuthRequired,
        ) is True


@pytest.mark.asyncio
async def test_silent_reconnect_auth_required_synthesizes_invalid_grant_and_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual MCPOLIS-BACKEND-C loop, exercised through
    ``refresh_token_for_user`` (the periodic path) rather than the
    reconnect path.

    The periodic GET drives the SDK into the authorization_code grant;
    our ``_noop_callback`` raises ``SilentReconnectAuthRequired``; the
    SDK logs ``OAuth flow error`` and re-raises. No refresh request was
    ever made, so the real provider's ``last_refresh_failure`` stays
    ``None`` — which is why the pre-fix periodic path fell through to the
    ``refresh.unchanged`` DEBUG branch and left the dead token in storage.
    The token then re-qualified every ``TOKEN_REFRESH_INTERVAL`` forever
    (prod: 314 ``refresh.started`` / 0 deletions for the ``meerbot``
    upstream over two days, one Sentry ``OAuth flow error`` per tick).

    Post-fix: the swallowed exception is inspected, an ``invalid_grant``
    signature is synthesized (mirroring ``_classify_reconnect_failure``),
    and the token is deleted so the pair drops out of
    ``get_all_stored_tokens`` and the loop converges. No
    ``_build_oauth_provider`` monkeypatch here — the real provider keeps
    ``last_refresh_failure=None``, which is the whole point.
    """
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        SilentReconnectAuthRequired,
    )

    store = FileConnectionStore(tmp_path)
    await _seed(store)

    _install_fake_client(
        monkeypatch,
        [SilentReconnectAuthRequired(
            "unexpected callback during silent reconnect",
        )],
    )
    _install_fake_sleep(monkeypatch)

    with structlog.testing.capture_logs() as logs:
        await refresh_token_for_user(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            user_id=USER_ID,
            connection_store=store,
            server_url=SERVER_URL,
        )

    # The dead token is deleted → next tick's get_all_stored_tokens no
    # longer yields the pair → the loop converges.
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None
    # Counter reset after the delete decision.
    assert await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is None
    # The synthesis log fires (the discriminator that says "we converted
    # a no-signature SilentReconnectAuthRequired into invalid_grant").
    assert [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.synthesized_invalid_grant"
    ], f"expected synthesized_invalid_grant log, got: {logs}"
    # ...routing into the shared silent_rejection + tokens_deleted contract.
    assert [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.silent_rejection"
        and e.get("error_code") == "invalid_grant"
    ], f"expected silent_rejection log, got: {logs}"
    assert [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.tokens_deleted"
        and e.get("reason") == "invalid_grant"
    ], f"expected tokens_deleted log, got: {logs}"


@pytest.mark.asyncio
async def test_silent_reconnect_auth_required_detected_inside_exception_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP SDK surfaces auth-flow failures through anyio task groups
    as ``ExceptionGroup``. The periodic path must detect the marker
    *inside* the group, not just on a bare re-raise — otherwise the real
    prod shape slips past the synthesis branch and the loop never
    converges. Pins the ``_exception_chain_contains`` integration at the
    ``refresh_token_for_user`` boundary (the helper-level test pins the
    walk in isolation)."""
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        SilentReconnectAuthRequired,
    )

    store = FileConnectionStore(tmp_path)
    await _seed(store)

    _install_fake_client(
        monkeypatch,
        [BaseExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            [SilentReconnectAuthRequired(
                "unexpected callback during silent reconnect",
            )],
        )],
    )
    _install_fake_sleep(monkeypatch)

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None


@pytest.mark.asyncio
async def test_invalid_grant_notifies_user_inline_before_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the §5.2 notifier wired in, an ``invalid_grant`` verdict must
    send the re-auth email AND delete the token. The send has to happen
    while the signature + token still exist — the delete (and
    ``reset_refresh_failures``) tears down exactly the state the notifier
    reads, so a send deferred to the hourly sweep would find nothing.
    Proven by a ``StubEmailSender`` recording one message and the token
    being gone afterward."""
    from mcp.client.auth import OAuthClientProvider

    from mcpolis.adapters.email.stub_email_sender import StubEmailSender
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        RefreshFailureSignature,
        _InitializingOAuthClientProvider,
        _build_oauth_provider,
    )

    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_fake_client(monkeypatch, [None])
    _install_fake_sleep(monkeypatch)

    real_build = _build_oauth_provider
    now = datetime.now(UTC)

    async def _build_with_sig(*args: Any, **kwargs: Any) -> OAuthClientProvider:
        provider = await real_build(*args, **kwargs)
        if isinstance(provider, _InitializingOAuthClientProvider):
            provider.last_refresh_failure = RefreshFailureSignature(
                status_code=400,
                body_excerpt='{"error":"invalid_grant"}',
                error_code="invalid_grant",
                timestamp=now,
            )
        return provider

    monkeypatch.setattr(oauth_refresh, "_build_oauth_provider", _build_with_sig)

    sender = StubEmailSender()

    async def _resolver(_org_id: str) -> list[str]:
        return ["admin@example.invalid"]

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),  # admin_oauth → resolver supplies recipient
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
        email_sender=sender,
        admin_email_resolver=_resolver,
        hmac_key=b"test-hmac-key",
    )

    # Exactly one re-auth email, to the resolved admin recipient.
    assert len(sender.sent) == 1
    assert sender.sent[0].to == "admin@example.invalid"
    # Token still deleted — the loop converges.
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None
    # Notified marker set so the hourly sweep won't double-send.
    assert await store.was_notified(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is True


@pytest.mark.asyncio
async def test_silent_reconnect_auth_required_notifies_user_inline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full meerbot fix: a ``SilentReconnectAuthRequired`` with no
    captured signature is synthesized into ``invalid_grant``, which now
    emails the user *and* deletes the token. Before this, the loop spun
    forever AND the user was never told — this asserts both halves are
    fixed in one pass."""
    from mcpolis.adapters.email.stub_email_sender import StubEmailSender
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        SilentReconnectAuthRequired,
    )

    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_fake_client(
        monkeypatch,
        [SilentReconnectAuthRequired(
            "unexpected callback during silent reconnect",
        )],
    )
    _install_fake_sleep(monkeypatch)

    sender = StubEmailSender()

    async def _resolver(_org_id: str) -> list[str]:
        return ["admin@example.invalid"]

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
        email_sender=sender,
        admin_email_resolver=_resolver,
        hmac_key=b"test-hmac-key",
    )

    assert len(sender.sent) == 1
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None


@pytest.mark.asyncio
async def test_invalid_grant_notify_error_is_swallowed_and_token_still_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inline-notify guard is best-effort. Note *what* can actually
    raise: per-recipient send failures are swallowed inside
    ``check_and_notify_upstream`` itself, so the realistic way to make
    the notify path raise is the ``admin_email_resolver`` (a policy /
    membership lookup) — exercised here. When it does, the error is
    logged as ``notify_failed`` and the token is STILL deleted. A flaky
    lookup must not strand the doomed token in storage and keep the
    10-min loop (and its per-tick Sentry ``OAuth flow error``) alive."""
    from mcp.client.auth import OAuthClientProvider

    from mcpolis.adapters.email.stub_email_sender import StubEmailSender
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        RefreshFailureSignature,
        _InitializingOAuthClientProvider,
        _build_oauth_provider,
    )

    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_fake_client(monkeypatch, [None])
    _install_fake_sleep(monkeypatch)

    real_build = _build_oauth_provider
    now = datetime.now(UTC)

    async def _build_with_sig(*args: Any, **kwargs: Any) -> OAuthClientProvider:
        provider = await real_build(*args, **kwargs)
        if isinstance(provider, _InitializingOAuthClientProvider):
            provider.last_refresh_failure = RefreshFailureSignature(
                status_code=400,
                body_excerpt='{"error":"invalid_grant"}',
                error_code="invalid_grant",
                timestamp=now,
            )
        return provider

    monkeypatch.setattr(oauth_refresh, "_build_oauth_provider", _build_with_sig)

    async def _boom_resolver(_org_id: str) -> list[str]:
        raise RuntimeError("policy engine unavailable")

    with structlog.testing.capture_logs() as logs:
        await refresh_token_for_user(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),  # admin_oauth → resolver is consulted
            user_id=USER_ID,
            connection_store=store,
            server_url=SERVER_URL,
            email_sender=StubEmailSender(),
            admin_email_resolver=_boom_resolver,
            hmac_key=b"test-hmac-key",
        )

    # The notify failure was logged ...
    assert [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.notify_failed"
    ], f"expected notify_failed log, got: {logs}"
    # ... and the token was deleted anyway → the loop still converges.
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None


@pytest.mark.asyncio
async def test_generic_non_network_error_does_not_synthesize_or_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthesis is specific to ``SilentReconnectAuthRequired``. A
    plain non-network error — no marker anywhere in the chain, no
    captured refresh signature — must NOT be converted into a synthesized
    ``invalid_grant``. The token stays put (the §5.1 transient-threshold
    policy, not this periodic loop, owns any eventual deletion for
    no-signature failures). Guards against a future broadening of the
    synthesis condition silently wiping tokens on ordinary errors."""
    store = FileConnectionStore(tmp_path)
    seeded = await _seed(store)
    _install_fake_client(monkeypatch, [RuntimeError("some unrelated boom")])
    _install_fake_sleep(monkeypatch)

    with structlog.testing.capture_logs() as logs:
        await refresh_token_for_user(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            user_id=USER_ID,
            connection_store=store,
            server_url=SERVER_URL,
        )

    # Neither the synthesis nor the delete fired.
    assert not [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.synthesized_invalid_grant"
    ], f"unexpected synthesis on a generic error: {logs}"
    assert not [
        e for e in logs
        if e.get("event") == "oauth.token.refresh.tokens_deleted"
    ], f"unexpected delete on a generic error: {logs}"
    # Token survives unchanged — this was not a definitive verdict.
    surviving = await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    )
    assert surviving is not None
    assert surviving.access_token == seeded.access_token
    # And no failure was recorded (no signature → nothing to record here).
    assert await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is None


@pytest.mark.asyncio
async def test_refresh_unchanged_is_logged_at_info_not_debug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``oauth.token.refresh.unchanged`` is the periodic loop's "we tried,
    nothing happened" outcome — i.e., the GET completed without an auth
    flow, no rotation, no signature, no marker. Before this change the
    branch logged at DEBUG, which the prod Vector pipeline doesn't forward
    to Elastic; the MCPOLIS-BACKEND-C investigation had to *infer* it from
    absence-of-WARNING. INFO makes ``started`` followed by silence
    impossible to miss next time. Regression-guard against reverting."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)
    # Benign GET (returns None) — no exception, no rotation, no signature
    # → falls through to the ``unchanged`` else-branch.
    _install_fake_client(monkeypatch, [None])
    _install_fake_sleep(monkeypatch)

    with structlog.testing.capture_logs() as logs:
        await refresh_token_for_user(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            user_id=USER_ID,
            connection_store=store,
            server_url=SERVER_URL,
        )

    unchanged = [
        e for e in logs if e.get("event") == "oauth.token.refresh.unchanged"
    ]
    assert unchanged, f"expected unchanged event, got: {logs}"
    assert unchanged[0].get("log_level") == "info", (
        "unchanged must be INFO so it reaches Elastic; got "
        f"log_level={unchanged[0].get('log_level')!r}"
    )


@pytest.mark.asyncio
async def test_distributed_lock_released_after_refresh_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock must be released even when the retry loop exhausts all
    attempts — otherwise a single cloud-mode backend failure would
    hold the lock for the full 60s TTL and block every other backend
    from trying."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)

    _install_fake_client(
        monkeypatch,
        [httpx.ConnectError("sim")] * TOKEN_REFRESH_MAX_RETRIES,
    )
    _install_fake_sleep(monkeypatch)

    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.release = AsyncMock()

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
        distributed_lock=lock,
    )

    lock.acquire.assert_awaited_once()
    lock.release.assert_awaited_once()
