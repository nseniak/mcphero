"""Tests for §5.1 — less-trigger-happy token deletion.

Motivation (``internal/documents/oauth-durability.md`` §5.1): the outer
``except`` in ``reconnect_with_stored_tokens`` historically wiped the
stored refresh token on any unhandled error. That was correct for a
genuine ``invalid_grant`` from the upstream, but every transient 5xx
or network blip also wiped the user out. A single bad minute of
network turned into a forced re-auth.

This module pins two behaviors:

1. ``_should_delete_on_refresh_failure`` — the pure policy
   function that branches on §5.4's captured signature plus the
   consecutive-failure counter. Tested in isolation.

2. The full ``reconnect_with_stored_tokens`` path — driven with a
   real ``FileConnectionStore`` and a stubbed ``UpstreamClientManager``,
   plus a ``_build_oauth_provider`` monkeypatched to return a
   provider that pre-populates ``last_refresh_failure``. Pins the
   delete-vs-retry observable behavior: the token row in storage
   either survives or doesn't, and the failure counter increments
   and resets appropriately.

Design choice: we go through the real ``FileConnectionStore`` rather
than mocking the counter methods so the test proves the full loop
(signature persists → counter increments → next call reads the new
count → threshold fires).  §8 of the durability doc warns against
lighter tests that silently pass for a fix that doesn't actually
work end-to-end — same rationale.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.client.auth import OAuthClientProvider

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services import upstream_connection_service
from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
    MAX_CONSECUTIVE_TRANSIENT_FAILURES,
    MIN_TRANSIENT_FAILURE_WINDOW_SECONDS,
    DisconnectReason,
    RefreshFailureSignature,
    _InitializingOAuthClientProvider,
    _should_delete_on_refresh_failure,
    reconnect_with_stored_tokens,
)
from tests.unit.factories import (
    make_oauth_upstream,
    make_refresh_failure_signature,
    seed_oauth_storage,
)


UPSTREAM_ID = "notion"
USER_ID = "__admin__"
UPSTREAM_URL = "https://mcp.example.invalid/mcp"
SERVER_URL = "https://gateway.example.invalid"
CALLBACK_URL = f"{SERVER_URL}/api/oauth/upstream/callback"


# Module-local alias so test bodies stay readable (``AuthMode.admin_oauth``
# is the default in the factory, but the two test cases below exercise
# both OAuth modes).
def _make_upstream() -> UpstreamDefinition:
    return make_oauth_upstream(id=UPSTREAM_ID, display_name="Notion")


def _make_signature(error_code: str | None) -> RefreshFailureSignature:
    return make_refresh_failure_signature(error_code=error_code)


# ── Policy-level unit tests ──────────────────────────────────────────


def test_invalid_grant_deletes_on_first_failure() -> None:
    """A signature with ``error_code == "invalid_grant"`` is the
    upstream explicitly telling us the refresh token is dead. Never
    retry that — delete immediately, regardless of counter."""
    assert _should_delete_on_refresh_failure(
        signature=_make_signature("invalid_grant"),
        failure_count=1,
        first_failure_at=datetime.now(UTC),
    ) is True


def test_transient_failure_below_threshold_keeps_tokens() -> None:
    """Typical transient case: a 5xx shows up once or twice then the
    upstream recovers. Deleting here would force re-auth over a blip
    — the §3.1 rotation-race and §3.2 boundary hazards combined
    aren't worth a single-strike delete."""
    assert _should_delete_on_refresh_failure(
        signature=_make_signature(None),
        failure_count=MAX_CONSECUTIVE_TRANSIENT_FAILURES - 1,
        first_failure_at=datetime.now(UTC) - timedelta(hours=1),
    ) is False


def test_transient_threshold_hit_but_window_short_keeps_tokens() -> None:
    """Five failures in two minutes = the upstream had a brief outage
    and may have already recovered. The window requirement forces a
    sustained-outage signal (≥ 30 min by default) before we wipe."""
    assert _should_delete_on_refresh_failure(
        signature=_make_signature(None),
        failure_count=MAX_CONSECUTIVE_TRANSIENT_FAILURES,
        first_failure_at=datetime.now(UTC) - timedelta(minutes=2),
    ) is False


def test_transient_threshold_and_window_both_hit_deletes() -> None:
    """Five failures over 30+ minutes = the upstream is probably
    genuinely gone for this user. At that point it's safer to wipe
    and force a clean re-auth than to keep retrying the dead token
    forever."""
    now = datetime.now(UTC)
    assert _should_delete_on_refresh_failure(
        signature=_make_signature(None),
        failure_count=MAX_CONSECUTIVE_TRANSIENT_FAILURES,
        first_failure_at=now - timedelta(
            seconds=MIN_TRANSIENT_FAILURE_WINDOW_SECONDS + 1,
        ),
        now=now,
    ) is True


def test_missing_signature_respects_threshold_and_window() -> None:
    """Some failure paths never reach ``_handle_refresh_response``
    (anyio cancel scope, decrypt error before the OAuth flow, etc.)
    so the signature is ``None``. Treat that as "unknown, assume
    transient" — the counter still gates deletion."""
    now = datetime.now(UTC)
    # Below threshold: keep.
    assert _should_delete_on_refresh_failure(
        signature=None,
        failure_count=MAX_CONSECUTIVE_TRANSIENT_FAILURES - 1,
        first_failure_at=now - timedelta(hours=1),
        now=now,
    ) is False
    # Above threshold + window: delete.
    assert _should_delete_on_refresh_failure(
        signature=None,
        failure_count=MAX_CONSECUTIVE_TRANSIENT_FAILURES,
        first_failure_at=now - timedelta(
            seconds=MIN_TRANSIENT_FAILURE_WINDOW_SECONDS + 1,
        ),
        now=now,
    ) is True


def test_missing_first_failure_at_keeps_tokens() -> None:
    """Defensive: if the counter is non-zero but ``first_failure_at``
    is ``None`` (shouldn't happen after ``record_refresh_failure``,
    but guard anyway) treat as "unknown window → retry rather than
    delete." A stale-state bug should not wipe tokens."""
    assert _should_delete_on_refresh_failure(
        signature=None,
        failure_count=MAX_CONSECUTIVE_TRANSIENT_FAILURES + 10,
        first_failure_at=None,
    ) is False


# ── Integration: drive ``reconnect_with_stored_tokens`` ───────────────


async def _seed(
    store: FileConnectionStore,
    *,
    expired: bool = True,
) -> "OAuthToken":
    """Thin wrapper over ``seed_oauth_storage`` that keeps the
    existing ``expired`` kwarg shape local tests use."""
    return await seed_oauth_storage(
        store,
        upstream_id=UPSTREAM_ID,
        user_id=USER_ID,
        callback_url=CALLBACK_URL,
        expires_in_minutes=-5 if expired else 30,
    )


def _install_failing_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    signature: RefreshFailureSignature | None,
) -> None:
    """Wire the service to:

    1. Skip the Step-1 HTTP trigger (the refresh trigger) by stubbing
       ``httpx.AsyncClient`` to raise a benign ``RuntimeError`` that
       the Step-1 ``except Exception: pass`` swallows.
    2. Make ``_build_oauth_provider`` return a provider with the given
       ``last_refresh_failure`` preseeded — that's the signal the
       outer except would have read from a real refresh response.
    3. Make the client manager's ``connect_upstream_for_user`` raise
       (simulating Step-3 connection failure after a rejected refresh).
    """
    class _DummyClient:
        def __init__(self, **_kw: Any) -> None: ...
        async def __aenter__(self) -> "_DummyClient": return self
        async def __aexit__(self, *_a: Any) -> None: ...
        async def get(self, _url: str) -> Any:
            # Benign — the Step-1 try/except already discards.
            raise RuntimeError("step1 simulated")

    monkeypatch.setattr(
        "mcpolis.domain.services.upstream_connection_service.httpx.AsyncClient",
        _DummyClient,
    )

    real_build = upstream_connection_service._build_oauth_provider  # pyright: ignore[reportPrivateUsage]

    async def _build_with_signature(
        *args: Any, **kwargs: Any,
    ) -> OAuthClientProvider:
        provider = await real_build(*args, **kwargs)
        if isinstance(provider, _InitializingOAuthClientProvider):
            provider.last_refresh_failure = signature
        return provider

    monkeypatch.setattr(
        upstream_connection_service,
        "_build_oauth_provider",
        _build_with_signature,
    )


def _make_failing_client_manager() -> UpstreamClientManager:
    cm = UpstreamClientManager(upstreams=[])
    cm.connect_upstream_for_user = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("step3 simulated reject"),
    )
    return cm


@pytest.mark.asyncio
async def test_reconnect_transient_failure_keeps_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First transient failure (5xx) must leave the stored token row
    intact and bump the counter to 1. The next retry cycle still has
    a token to try."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_failing_reconnect(monkeypatch, _make_signature(None))

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=_make_failing_client_manager(),
        server_url=SERVER_URL,
    )

    assert result is DisconnectReason.token_refresh_failed
    # Token must survive — this was transient.
    surviving = await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    )
    assert surviving is not None
    # Counter at 1.
    failures = await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    )
    assert failures is not None
    count, _first_at = failures
    assert count == 1


@pytest.mark.asyncio
async def test_reconnect_invalid_grant_deletes_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invalid_grant`` is the upstream saying "this refresh token
    is dead" — no amount of retries will revive it. Delete on the
    first occurrence so the user gets a prompt re-auth flow instead
    of five cycles of doomed retries."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_failing_reconnect(
        monkeypatch, _make_signature("invalid_grant"),
    )

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=_make_failing_client_manager(),
        server_url=SERVER_URL,
    )

    assert result is DisconnectReason.token_refresh_failed
    # Token deleted.
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None
    # Counter cleared — we made our decision, don't carry state.
    assert await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is None


@pytest.mark.asyncio
async def test_reconnect_transient_threshold_eventually_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate ``MAX_CONSECUTIVE_TRANSIENT_FAILURES`` consecutive
    transient failures with a sustained ``first_failure_at`` old
    enough to cross the window. On the Nth call the token must be
    deleted — this is the "the upstream really is down for you"
    backstop."""
    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_failing_reconnect(monkeypatch, _make_signature(None))

    # Backdate the initial failure record so subsequent increments
    # land outside the window by the time we hit threshold count.
    old = datetime.now(UTC) - timedelta(
        seconds=MIN_TRANSIENT_FAILURE_WINDOW_SECONDS + 60,
    )
    key = FileConnectionStore._failures_key(UPSTREAM_ID, USER_ID)  # pyright: ignore[reportPrivateUsage]
    async with store._lock:  # pyright: ignore[reportPrivateUsage]
        data = store._read()  # pyright: ignore[reportPrivateUsage]
        data[key] = {
            "count": MAX_CONSECUTIVE_TRANSIENT_FAILURES - 1,
            "first_failure_at": old.isoformat(),
        }
        store._write(data)  # pyright: ignore[reportPrivateUsage]

    # The Nth failure crosses the threshold.
    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=_make_failing_client_manager(),
        server_url=SERVER_URL,
    )

    assert result is DisconnectReason.token_refresh_failed
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None
    # Counter reset after delete — clean slate for next re-auth.
    assert await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is None


@pytest.mark.asyncio
async def test_reconnect_success_resets_failure_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful reconnect after a transient-failure burst must
    reset the counter to zero. Without this, the next unrelated
    blip months later would start from a poisoned count and delete
    tokens unfairly fast."""
    store = FileConnectionStore(tmp_path)
    # Seed with a non-expired token so reconnect_with_stored_tokens
    # doesn't need to refresh, then wire the client manager to succeed.
    await _seed(store, expired=False)
    # Preexisting transient burst from earlier in the day.
    async with store._lock:  # pyright: ignore[reportPrivateUsage]
        data = store._read()  # pyright: ignore[reportPrivateUsage]
        data[FileConnectionStore._failures_key(UPSTREAM_ID, USER_ID)] = {  # pyright: ignore[reportPrivateUsage]
            "count": 3,
            "first_failure_at": datetime.now(UTC).isoformat(),
        }
        store._write(data)  # pyright: ignore[reportPrivateUsage]

    cm = UpstreamClientManager(upstreams=[])
    cm.connect_upstream_for_user = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # No network in Step 1 — dummy client that no-ops.
    class _NoopClient:
        def __init__(self, **_kw: Any) -> None: ...
        async def __aenter__(self) -> "_NoopClient": return self
        async def __aexit__(self, *_a: Any) -> None: ...
        async def get(self, _url: str) -> Any:
            return None

    monkeypatch.setattr(
        "mcpolis.domain.services.upstream_connection_service.httpx.AsyncClient",
        _NoopClient,
    )

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=cm,
        server_url=SERVER_URL,
    )

    # Pre-seed the per-upstream connection-error row and the per-user
    # "user has been notified" flag — both belong to the prior failure
    # burst and must be wiped by the success path so a fresh failure
    # later can re-arm the email pipeline and the dashboard reads as
    # connected.
    await store.set_connection_error(
        DEFAULT_ORG_ID, UPSTREAM_ID,
        DisconnectReason.token_refresh_failed,
    )
    await store.mark_notified(DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=cm,
        server_url=SERVER_URL,
    )

    assert result is None  # Success
    # Counter reset.
    assert await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is None
    # Connection-error row cleared (otherwise the dashboard's
    # ``disconnect_reason`` column lies "re-auth needed" forever).
    assert await store.get_connection_error(
        DEFAULT_ORG_ID, UPSTREAM_ID,
    ) is None
    # Notified flag cleared so the §5.2 email pipeline can re-arm on
    # the next failure burst (otherwise we silently never email
    # again for this user/upstream pair).
    assert await store.was_notified(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is False


# ── Fix #2: SilentReconnectAuthRequired → synthesized invalid_grant ──


@pytest.mark.asyncio
async def test_silent_reconnect_auth_required_synthesizes_invalid_grant_and_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK's 401 handler falls into authorization_code grant
    during a silent reconnect, our ``_noop_callback`` raises
    ``SilentReconnectAuthRequired``. The outer except detects this
    even when wrapped in an ``ExceptionGroup`` (anyio task groups)
    and synthesizes an ``invalid_grant`` signature so §5.1 deletes
    the token immediately and §5.2's email pipeline notifies the
    user — instead of accumulating five identical "transient"
    failures over half an hour while the user wonders what's wrong.

    This test replicates the 2026-04-25 Mixpanel incident: the probe
    tore down a zombie session, the reconnect's first request got
    401, the SDK skipped refresh and went straight to authorization_code,
    our ``_noop_callback`` fired. Pre-fix outcome was ``tokens_kept``
    with no signal. Post-fix outcome is ``tokens_deleted`` with
    ``error_code="invalid_grant"`` and a notify-eligible signature
    on the failure row.
    """
    from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
        SilentReconnectAuthRequired,
    )

    store = FileConnectionStore(tmp_path)
    await _seed(store)

    # Step-1 trigger: benign — just lets reconnect proceed to Step 3.
    class _DummyClient:
        def __init__(self, **_kw: Any) -> None: ...
        async def __aenter__(self) -> "_DummyClient": return self
        async def __aexit__(self, *_a: Any) -> None: ...
        async def get(self, _url: str) -> Any:
            return None

    monkeypatch.setattr(
        "mcpolis.domain.services.upstream_connection_service.httpx.AsyncClient",
        _DummyClient,
    )

    # Step 3: simulate the SDK falling into authorization_code grant
    # by raising the marker exception wrapped in an ExceptionGroup
    # (the anyio shape that bit us in prod).
    cm = UpstreamClientManager(upstreams=[])
    cm.connect_upstream_for_user = AsyncMock(  # type: ignore[method-assign]
        side_effect=BaseExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            [SilentReconnectAuthRequired(
                "unexpected callback during silent reconnect",
            )],
        ),
    )

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=cm,
        server_url=SERVER_URL,
    )

    assert result is DisconnectReason.token_refresh_failed
    # Token MUST be deleted (synthesized invalid_grant → §5.1 deletes).
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is None
    # The synthesized signature is persisted on the per-upstream error
    # row so an operator running ``db.connections.find`` sees it.
    error_signature = await store.get_connection_error_signature(
        DEFAULT_ORG_ID, UPSTREAM_ID,
    )
    assert error_signature is not None
    assert error_signature["error_code"] == "invalid_grant"
    # Counter cleared after delete.
    assert await store.get_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    ) is None


# ── Structured-log contract for ``_classify_reconnect_failure`` ──────
#
# The ``tokens_kept`` and ``tokens_deleted`` log lines are part of the
# operator-visible contract: ops dashboards filter on
# ``failure_count`` / ``threshold_count`` / ``threshold_window_seconds``
# (kept) and ``reason`` (deleted) to triage stuck reconnect loops. A
# refactor that drops or renames a field would break those queries
# without any state-level assertion going red, so we pin the field
# names + shapes here.


@pytest.mark.asyncio
async def test_tokens_kept_log_emits_threshold_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below-threshold transient failure must emit ``tokens_kept`` with
    the structured fields operators grep on (``failure_count``,
    ``elapsed_seconds``, ``threshold_count``, ``threshold_window_seconds``).
    Without these, the §5.4 dashboard "stuck-reconnect alarms" query
    returns nothing and the operator has no signal."""
    import structlog

    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_failing_reconnect(monkeypatch, _make_signature(None))

    with structlog.testing.capture_logs() as logs:
        result = await reconnect_with_stored_tokens(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            effective_user=USER_ID,
            connection_store=store,
            client_manager=_make_failing_client_manager(),
            server_url=SERVER_URL,
        )

    assert result is DisconnectReason.token_refresh_failed
    kept = [
        e for e in logs
        if e.get("event") == "upstream.reconnect.tokens_kept"
    ]
    assert kept, f"expected upstream.reconnect.tokens_kept event, got: {logs}"
    event = kept[0]
    assert event["upstream_id"] == UPSTREAM_ID
    assert event["user"] == USER_ID
    assert event["org_id"] == DEFAULT_ORG_ID
    assert event["failure_count"] == 1
    assert event["threshold_count"] == MAX_CONSECUTIVE_TRANSIENT_FAILURES
    assert event["threshold_window_seconds"] == (
        MIN_TRANSIENT_FAILURE_WINDOW_SECONDS
    )
    assert isinstance(event["elapsed_seconds"], int)
    assert event["elapsed_seconds"] >= 0


@pytest.mark.asyncio
async def test_tokens_deleted_log_emits_reason_invalid_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invalid_grant`` deletes immediately and the log must spell out
    ``reason="invalid_grant"`` so the operator dashboard can split
    "user revoked / token rotated" deletes from threshold-driven ones.
    The discriminator is what makes the line useful; without it the
    two delete paths are indistinguishable."""
    import structlog

    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_failing_reconnect(monkeypatch, _make_signature("invalid_grant"))

    with structlog.testing.capture_logs() as logs:
        await reconnect_with_stored_tokens(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            effective_user=USER_ID,
            connection_store=store,
            client_manager=_make_failing_client_manager(),
            server_url=SERVER_URL,
        )

    deleted = [
        e for e in logs
        if e.get("event") == "upstream.reconnect.tokens_deleted"
    ]
    assert deleted, (
        f"expected upstream.reconnect.tokens_deleted event, got: {logs}"
    )
    event = deleted[0]
    assert event["upstream_id"] == UPSTREAM_ID
    assert event["user"] == USER_ID
    assert event["org_id"] == DEFAULT_ORG_ID
    assert event["reason"] == "invalid_grant"


@pytest.mark.asyncio
async def test_tokens_deleted_log_emits_reason_transient_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transient-threshold delete path uses
    ``reason="transient-threshold (failures=N)"`` so an operator
    investigating a sustained-outage delete can see the count without
    cross-referencing the failure-row state. The string shape is part
    of the contract — dashboards regex on ``transient-threshold``."""
    import structlog

    store = FileConnectionStore(tmp_path)
    await _seed(store)
    _install_failing_reconnect(monkeypatch, _make_signature(None))

    # Backdate the failure record so this Nth call lands outside the
    # window with count == threshold.
    old = datetime.now(UTC) - timedelta(
        seconds=MIN_TRANSIENT_FAILURE_WINDOW_SECONDS + 60,
    )
    key = FileConnectionStore._failures_key(UPSTREAM_ID, USER_ID)  # pyright: ignore[reportPrivateUsage]
    async with store._lock:  # pyright: ignore[reportPrivateUsage]
        data = store._read()  # pyright: ignore[reportPrivateUsage]
        data[key] = {
            "count": MAX_CONSECUTIVE_TRANSIENT_FAILURES - 1,
            "first_failure_at": old.isoformat(),
        }
        store._write(data)  # pyright: ignore[reportPrivateUsage]

    with structlog.testing.capture_logs() as logs:
        await reconnect_with_stored_tokens(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(),
            effective_user=USER_ID,
            connection_store=store,
            client_manager=_make_failing_client_manager(),
            server_url=SERVER_URL,
        )

    deleted = [
        e for e in logs
        if e.get("event") == "upstream.reconnect.tokens_deleted"
    ]
    assert deleted, (
        f"expected upstream.reconnect.tokens_deleted event, got: {logs}"
    )
    event = deleted[0]
    assert event["reason"] == (
        f"transient-threshold (failures={MAX_CONSECUTIVE_TRANSIENT_FAILURES})"
    )


# ── Edge branches in ``reconnect_with_stored_tokens`` ────────────────


@pytest.mark.asyncio
async def test_reconnect_timeout_returns_connection_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Step-3 connect that exceeds ``timeout`` must surface as
    ``DisconnectReason.connection_timeout``, not the generic
    ``token_refresh_failed``. Operators triage timeouts vs auth
    rejections differently — collapsing them would mask "the
    upstream is slow" as "user must re-auth" in the admin UI."""
    store = FileConnectionStore(tmp_path)
    await _seed(store, expired=False)

    # No-op Step 1 so reconnect proceeds to Step 3.
    class _NoopClient:
        def __init__(self, **_kw: Any) -> None: ...
        async def __aenter__(self) -> "_NoopClient": return self
        async def __aexit__(self, *_a: Any) -> None: ...
        async def get(self, _url: str) -> Any: return None

    monkeypatch.setattr(
        "mcpolis.domain.services.upstream_connection_service.httpx.AsyncClient",
        _NoopClient,
    )

    cm = UpstreamClientManager(upstreams=[])
    cm.connect_upstream_for_user = AsyncMock(  # type: ignore[method-assign]
        side_effect=TimeoutError("simulated step3 timeout"),
    )

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=cm,
        server_url=SERVER_URL,
    )

    assert result is DisconnectReason.connection_timeout
    # Connection-error row carries the same reason so the dashboard
    # column matches the function's return value.
    assert await store.get_connection_error(
        DEFAULT_ORG_ID, UPSTREAM_ID,
    ) == DisconnectReason.connection_timeout.value
    # Stored token must NOT be deleted on a timeout — that would
    # force re-auth over what may be an upstream-side slowness.
    assert await store.get_user_token(
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
    ) is not None


@pytest.mark.asyncio
async def test_reconnect_no_tokens_after_silent_refresh_returns_token_refresh_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Step 1's silent refresh wipes the stored token row (e.g. the
    SDK got a 4xx and discarded it) AND the access token was already
    expired, the only honest classification is ``token_refresh_failed``
    — refresh was attempted and yielded nothing usable. The
    ``token_expired`` reason is reserved for the case where access was
    *still live* before the refresh, which is the next test."""
    store = FileConnectionStore(tmp_path)
    await _seed(store, expired=True)

    # Step 1's "lightweight HTTP" client deletes the stored token row
    # to mimic the SDK reacting to a 4xx-on-refresh.
    class _ClearingClient:
        def __init__(self, **_kw: Any) -> None: ...
        async def __aenter__(self) -> "_ClearingClient": return self
        async def __aexit__(self, *_a: Any) -> None: ...
        async def get(self, _url: str) -> Any:
            await store.delete_user_token(
                DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
            )
            return None

    monkeypatch.setattr(
        "mcpolis.domain.services.upstream_connection_service.httpx.AsyncClient",
        _ClearingClient,
    )

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=UpstreamClientManager(upstreams=[]),
        server_url=SERVER_URL,
    )

    assert result is DisconnectReason.token_refresh_failed


@pytest.mark.asyncio
async def test_reconnect_no_tokens_after_silent_refresh_with_live_access_returns_token_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Step 1's silent refresh wipes the stored row but the access
    token was still in-margin, returning ``token_refresh_failed`` would
    be misleading — the access didn't actually expire from the user's
    perspective; the storage just emptied. Distinguish with
    ``token_expired`` so the admin UI's prompt copy is accurate."""
    store = FileConnectionStore(tmp_path)
    await _seed(store, expired=False)

    class _ClearingClient:
        def __init__(self, **_kw: Any) -> None: ...
        async def __aenter__(self) -> "_ClearingClient": return self
        async def __aexit__(self, *_a: Any) -> None: ...
        async def get(self, _url: str) -> Any:
            await store.delete_user_token(
                DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID,
            )
            return None

    monkeypatch.setattr(
        "mcpolis.domain.services.upstream_connection_service.httpx.AsyncClient",
        _ClearingClient,
    )

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=UpstreamClientManager(upstreams=[]),
        server_url=SERVER_URL,
    )

    assert result is DisconnectReason.token_expired
