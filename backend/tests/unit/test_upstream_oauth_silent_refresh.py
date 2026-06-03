"""End-to-end tests for the silent OAuth refresh path.

Pins the contract that keeps our ``_noop_callback`` from being reached
by the SDK's silent-reconnect / periodic-refresh flows.

Background: the MCP SDK's ``OAuthContext._initialize`` loads stored
tokens but never calls ``update_token_expiry``. Left alone, that makes
``is_token_valid()`` treat an already-expired stored token as valid,
``async_auth_flow`` skips the refresh_token branch, the request goes
out with the stale token, the server responds 401, and the SDK falls
through to the authorization_code grant — which our silent-reconnect
path blocks via ``_noop_callback`` (RuntimeError in Sentry).

``_InitializingOAuthClientProvider`` in ``upstream_connection_service``
fixes this by calling ``update_token_expiry`` after ``_initialize``.
The earlier attempt at a fix wired a real ``expires_in`` through
``McpTokenStorage`` but tested only a hand-built ``OAuthContext`` with
``token_expiry_time`` pre-seeded — a shape the real ``_initialize``
never produces. These tests drive the real provider end-to-end so a
regression on that layer can't pass silently again.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
from tests.unit.factories import make_oauth_metadata
from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as InternalOAuthToken,
)
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
from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
    _build_oauth_provider,
    _noop_callback,
    _noop_redirect,
    _persist_discovered_oauth_metadata,
)

UPSTREAM_ID = "notion"
USER_ID = "__admin__"
UPSTREAM_URL = "https://mcp.example.invalid/mcp"
SERVER_URL = "https://gateway.example.invalid"
# Must match the callback URL ``_build_oauth_provider`` computes — if the
# stored client_info's redirect_uris don't include this, the provider's
# DCR self-heal path drops client_info and the SDK's ``can_refresh_token``
# returns False, silently skipping the branch we're trying to pin here.
CALLBACK_URL = f"{SERVER_URL}/api/oauth/upstream/callback"


def _make_upstream() -> UpstreamDefinition:
    """Per-user OAuth upstream with no pre-seeded client_id, so
    ``_build_oauth_provider`` neither writes nor rewrites client_info
    beyond what the test seeds explicitly."""
    return UpstreamDefinition(
        id=UPSTREAM_ID,
        display_name="Notion",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=UPSTREAM_URL),
        auth=UpstreamAuthConfig(mode=AuthMode.per_user_oauth),
    )


async def _seed_storage(
    storage: McpTokenStorage, *, expired: bool
) -> None:
    """Seed a stored token + client_info via the real storage adapter.

    Going through the adapter (rather than writing raw rows) exercises
    the same round-trip the production code performs, including the
    ``expires_at → expires_in`` translation in
    ``_internal_to_sdk_token``.
    """
    # The adapter converts SDK tokens (relative expires_in) into
    # internal tokens (absolute expires_at). We bypass the adapter's
    # ``set_tokens`` so we can pin an exact expires_at without racing
    # the clock.
    delta = timedelta(minutes=-5 if expired else 30)
    internal = InternalOAuthToken(
        access_token="stored-at",
        refresh_token="stored-rt",
        expires_at=datetime.now(UTC) + delta,
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )
    await storage._store.put_user_token(  # pyright: ignore[reportPrivateUsage]
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID, internal,
    )
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="cid",
        client_secret="csec",
        redirect_uris=[AnyUrl(CALLBACK_URL)],
        token_endpoint_auth_method="client_secret_post",
    ))


async def _build_provider_with_expired_token(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    await _seed_storage(storage, expired=True)
    return await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )


@pytest.mark.asyncio
async def test_initialize_marks_expired_stored_token_as_invalid(
    tmp_path: Path,
) -> None:
    """After the real ``_initialize`` runs, ``is_token_valid()`` must
    report False for an already-expired stored token. If this ever
    flips back to True, ``async_auth_flow`` will send the stale token,
    get 401, and fall into ``_perform_authorization_code_grant`` — the
    exact path that triggers our ``_noop_callback`` in prod."""
    provider = await _build_provider_with_expired_token(tmp_path)
    await provider._initialize()  # pyright: ignore[reportPrivateUsage]
    assert provider.context.is_token_valid() is False


@pytest.mark.asyncio
async def test_initialize_marks_live_stored_token_as_valid(
    tmp_path: Path,
) -> None:
    """Mirror of the expired-token test: a stored token that is not
    yet expired must come out of ``_initialize`` as valid, so the SDK
    attaches the Bearer and skips refresh."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    await _seed_storage(storage, expired=False)
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )
    await provider._initialize()  # pyright: ignore[reportPrivateUsage]
    assert provider.context.is_token_valid() is True


@pytest.mark.asyncio
async def test_async_auth_flow_yields_refresh_first_for_expired_token(
    tmp_path: Path,
) -> None:
    """The load-bearing integration test: with an expired stored token,
    the SDK's generator must emit a refresh_token POST before anything
    else. Anything else (original request, discovery GET, authorize
    redirect) means we're one hop away from ``_noop_callback``."""
    provider = await _build_provider_with_expired_token(tmp_path)

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    assert first.method == "POST", (
        f"expected refresh POST, got {first.method} {first.url}"
    )
    body = first.read()
    assert b"grant_type=refresh_token" in body
    assert b"refresh_token=stored-rt" in body


@pytest.mark.asyncio
async def test_noop_callback_never_reached_on_successful_refresh(
    tmp_path: Path,
) -> None:
    """Feed a fake 200 refresh response back into the generator and
    confirm the SDK retries the original request with the new bearer —
    never touching ``_perform_authorization_code_grant`` and therefore
    never invoking ``_noop_callback``."""
    provider = await _build_provider_with_expired_token(tmp_path)

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)

    refresh_req = await flow.__anext__()
    assert b"grant_type=refresh_token" in refresh_req.read()

    refresh_ok = httpx.Response(
        200,
        request=refresh_req,
        json={
            "access_token": "new-at",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "new-rt",
        },
    )
    retried = await flow.asend(refresh_ok)

    assert retried.method == "GET"
    assert str(retried.url) == UPSTREAM_URL
    assert retried.headers["authorization"] == "Bearer new-at"


# ── Proactive margin: within-margin (not yet expired) tokens ─────
# Pins the contract that makes our domain-level 20-min margin
# actually effective. With the margin wired through
# ``McpTokenStorage``, a stored token that's still "valid" by the
# SDK's zero-buffer clock but inside our margin must trigger the
# refresh branch *before* the request leaves the process. Without
# this, every expiry-boundary-crossing tool call is a race where
# the SDK's 401 handler (which runs ``authorization_code``, not
# ``refresh_token``, in our SDK version) can hit ``_noop_callback``
# and wipe the user's tokens.


async def _seed_storage_with_remaining(
    storage: McpTokenStorage, *, remaining_seconds: float,
) -> None:
    internal = InternalOAuthToken(
        access_token="stored-at",
        refresh_token="stored-rt",
        expires_at=datetime.now(UTC) + timedelta(seconds=remaining_seconds),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )
    await storage._store.put_user_token(  # pyright: ignore[reportPrivateUsage]
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID, internal,
    )
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="cid",
        client_secret="csec",
        redirect_uris=[AnyUrl(CALLBACK_URL)],
        token_endpoint_auth_method="client_secret_post",
    ))


@pytest.mark.asyncio
async def test_within_margin_token_triggers_refresh_before_original_request(
    tmp_path: Path,
) -> None:
    """Token has 15 min remaining, margin 20 min. Without the clamp,
    the SDK would accept the token as valid and send the original
    request. With the clamp, the SDK's ``is_token_valid()`` resolves
    to False after ``_initialize`` primes ``token_expiry_time``, so
    ``async_auth_flow`` issues a refresh POST first — same shape as
    the expired-token case."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
        refresh_margin_seconds=20 * 60,
    )
    await _seed_storage_with_remaining(storage, remaining_seconds=15 * 60)
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    assert first.method == "POST", (
        f"expected refresh POST, got {first.method} {first.url}"
    )
    body = first.read()
    assert b"grant_type=refresh_token" in body
    assert b"refresh_token=stored-rt" in body


@pytest.mark.asyncio
async def test_outside_margin_token_sends_original_request_directly(
    tmp_path: Path,
) -> None:
    """Token has 45 min remaining, margin 20 min. Should NOT refresh
    — sending a refresh every tick for a comfortably-fresh token
    would thrash upstream providers with one-time-use refresh tokens
    (Notion, etc.) and create more rotation-race opportunities, not
    fewer."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
        refresh_margin_seconds=20 * 60,
    )
    await _seed_storage_with_remaining(storage, remaining_seconds=45 * 60)
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    # First yield is the original GET, with the stored bearer attached.
    assert first.method == "GET"
    assert str(first.url) == UPSTREAM_URL
    assert first.headers["authorization"] == "Bearer stored-at"


@pytest.mark.asyncio
async def test_zero_margin_disables_proactive_behavior(
    tmp_path: Path,
) -> None:
    """Regression guard: a storage instance constructed without the
    margin (default 0.0) must preserve the SDK's native reactive-only
    refresh — a 15-min-remaining token goes out with the existing
    bearer. Protects callers that haven't opted into the margin from
    surprise extra refreshes."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    )  # default margin = 0.0
    await _seed_storage_with_remaining(storage, remaining_seconds=15 * 60)
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    assert first.method == "GET"
    assert first.headers["authorization"] == "Bearer stored-at"


# ── max_age clamp: stale-by-wall-clock tokens (§3.6 seatbelt) ────
# Pins the contract that the §3.6 seatbelt (refresh stale tokens
# regardless of declared expires_at) is wired all the way through.
# Initial implementation only put the max-age check in
# ``_token_needs_refresh`` (oauth_refresh) — the periodic loop
# decided "refresh!" but the SDK's ``async_auth_flow`` ignored that
# decision because ``is_token_valid()`` saw the token as good. Result
# was a dev-env bug that fired 77 ``refresh.started`` events in 13h
# with 0 ``storage.rotated`` events. These tests pin the clamp at
# the boundary the SDK actually consults.


async def _seed_storage_with_age(
    storage: McpTokenStorage,
    *,
    expires_in_seconds: float,
    age_seconds: float,
) -> None:
    """Like ``_seed_storage_with_remaining`` but also lets the test
    pin ``updated_at`` so the max-age clamp can be exercised. Bypasses
    ``put_user_token``'s auto-stamp (which would set ``updated_at`` to
    now) by writing the row directly via the underlying file store's
    private ``_write``."""
    import json as _json
    internal = InternalOAuthToken(
        access_token="stored-at",
        refresh_token="stored-rt",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )
    await storage._store.put_user_token(  # pyright: ignore[reportPrivateUsage]
        DEFAULT_ORG_ID, USER_ID, UPSTREAM_ID, internal,
    )
    # Backdate updated_at on disk to the requested age.
    file_store = storage._store  # pyright: ignore[reportPrivateUsage]
    raw = _json.loads(file_store._path.read_text())  # pyright: ignore[reportPrivateUsage]
    key = f"user:{UPSTREAM_ID}:{USER_ID}"
    raw[key]["updated_at"] = (
        datetime.now(UTC) - timedelta(seconds=age_seconds)
    ).isoformat()
    file_store._path.write_text(_json.dumps(raw, indent=2))  # pyright: ignore[reportPrivateUsage]
    await storage.set_client_info(OAuthClientInformationFull(
        client_id="cid",
        client_secret="csec",
        redirect_uris=[AnyUrl(CALLBACK_URL)],
        token_endpoint_auth_method="client_secret_post",
    ))


@pytest.mark.asyncio
async def test_stale_token_with_max_age_clamp_triggers_refresh(
    tmp_path: Path,
) -> None:
    """Token has 12h remaining (well outside any margin) but
    ``updated_at`` is 5h old (well past the 4h ``max_age`` threshold).
    Without the max-age clamp, the SDK would consider the token valid
    and skip refresh — exactly the bug from 2026-04-25. With the
    clamp, ``async_auth_flow`` issues a refresh POST first."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
        refresh_margin_seconds=20 * 60,  # margin won't trip (12h > 20min)
        max_age_seconds=4 * 60 * 60,
    )
    await _seed_storage_with_age(
        storage,
        expires_in_seconds=12 * 60 * 60,  # 12h declared TTL
        age_seconds=5 * 60 * 60,           # 5h since last rotation
    )
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    assert first.method == "POST", (
        f"expected refresh POST (max-age clamp), got {first.method} {first.url}"
    )
    body = first.read()
    assert b"grant_type=refresh_token" in body
    assert b"refresh_token=stored-rt" in body


@pytest.mark.asyncio
async def test_stale_token_without_max_age_clamp_skips_refresh(
    tmp_path: Path,
) -> None:
    """Regression guard: when the storage is constructed WITHOUT
    ``max_age_seconds``, a stale ``updated_at`` does NOT trigger
    refresh — even though our domain policy might want it to. This
    pins the contract that the storage clamp is the only thing that
    makes the SDK refresh; a caller that forgets to pass the
    parameter will silently skip rotations on stale tokens (which is
    exactly how we landed in the dev-env bug). Test exists so a
    future refactor that drops the parameter from
    ``oauth_refresh.refresh_token_for_user`` shows up as a failure
    here, not as a silent prod regression."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
        refresh_margin_seconds=20 * 60,
        # max_age_seconds intentionally omitted
    )
    await _seed_storage_with_age(
        storage,
        expires_in_seconds=12 * 60 * 60,
        age_seconds=5 * 60 * 60,
    )
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    # Original request goes out with the existing bearer — no refresh.
    assert first.method == "GET"
    assert first.headers["authorization"] == "Bearer stored-at"


@pytest.mark.asyncio
async def test_fresh_token_under_max_age_skips_refresh(
    tmp_path: Path,
) -> None:
    """Token is just-rotated (1h old) AND has plenty of TTL remaining
    (8h). Both clamps are no-op — original request goes out with the
    existing bearer. Pins the hot path: a freshly-rotated long-TTL
    token does NOT cause unnecessary rotations."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
        refresh_margin_seconds=20 * 60,
        max_age_seconds=4 * 60 * 60,
    )
    await _seed_storage_with_age(
        storage,
        expires_in_seconds=8 * 60 * 60,
        age_seconds=60 * 60,
    )
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    assert first.method == "GET"
    assert first.headers["authorization"] == "Bearer stored-at"


# ── oauth_metadata pre-population (§3.8 / §5.4) ────────────────────
# Pins the contract that the SDK's refresh branch posts to the
# *persisted* ``token_endpoint``, not to ``<base>/token``. Without
# this, a fresh process refreshing a Mixpanel-style upstream — whose
# real endpoint isn't at ``<base>/token`` — will 404 every periodic
# tick until the §3.7 airbag eventually catches it via probe + delete.
#
# The factory ``make_oauth_metadata`` deliberately seeds a
# ``token_endpoint`` on a different host from the upstream MCP base
# URL (``mcp.example.invalid`` vs ``oauth.example.invalid``). A fix
# that "works" only when the two are co-located would silently pass
# an asymmetric test; this geometry catches that.


@pytest.mark.asyncio
async def test_refresh_uses_persisted_oauth_metadata_token_endpoint(
    tmp_path: Path,
) -> None:
    """With persisted ``oauth_metadata``, the SDK's refresh POST goes
    to the metadata's ``token_endpoint``. Drives the real
    ``async_auth_flow`` and asserts on the URL of the first yielded
    request — same shape as the existing silent-refresh tests."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
    )
    await _seed_storage(storage, expired=True)
    metadata = make_oauth_metadata()
    await storage.set_oauth_metadata(metadata)

    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    assert first.method == "POST"
    # The persisted endpoint is ``oauth.example.invalid/oauth/token``
    # — distinct from the upstream MCP base ``mcp.example.invalid``.
    assert str(first.url) == str(metadata.token_endpoint)


@pytest.mark.asyncio
async def test_refresh_falls_back_to_base_token_when_no_metadata(
    tmp_path: Path,
) -> None:
    """Regression guard for legacy rows: when no ``oauth_metadata`` is
    persisted, the SDK falls back to ``<base>/token`` (the buggy
    behavior pre-§3.8). This pins the fall-back so a future refactor
    that drops the per-storage clamp doesn't silently change behavior
    for upstreams whose token endpoint really IS at ``<base>/token``."""
    provider = await _build_provider_with_expired_token(tmp_path)

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    first = await flow.__anext__()

    assert first.method == "POST"
    # ``UPSTREAM_URL`` is ``https://mcp.example.invalid/mcp``; the SDK
    # strips the path and uses ``<scheme>://<netloc>/token``.
    assert str(first.url) == "https://mcp.example.invalid/token"


@pytest.mark.asyncio
async def test_build_oauth_provider_loads_persisted_metadata(
    tmp_path: Path,
) -> None:
    """A fresh ``_build_oauth_provider`` (i.e. simulated process boot)
    finds the persisted metadata and pre-populates
    ``OAuthContext.oauth_metadata`` so ``_get_token_endpoint``
    resolves it without re-discovery."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    metadata = make_oauth_metadata()
    await storage.set_oauth_metadata(metadata)

    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )

    assert provider.context.oauth_metadata is not None
    assert (
        str(provider.context.oauth_metadata.token_endpoint)
        == str(metadata.token_endpoint)
    )


@pytest.mark.asyncio
async def test_get_oauth_metadata_returns_none_when_unset(
    tmp_path: Path,
) -> None:
    """Sanity: a virgin storage row reports no metadata and
    ``_build_oauth_provider`` leaves ``context.oauth_metadata`` unset
    so the SDK's discovery branches still run when needed."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)

    assert await storage.get_oauth_metadata() is None
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )
    assert provider.context.oauth_metadata is None


@pytest.mark.asyncio
async def test_set_oauth_metadata_roundtrips_through_storage(
    tmp_path: Path,
) -> None:
    """Pin the SDK ↔ storage round-trip so a future SDK bump that
    changes ``OAuthMetadata`` field shapes (Pydantic validation)
    surfaces here, not silently in production."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    metadata = make_oauth_metadata(
        token_endpoint="https://oauth.example.invalid/v1/token",
    )

    await storage.set_oauth_metadata(metadata)
    loaded = await storage.get_oauth_metadata()

    assert loaded is not None
    assert str(loaded.token_endpoint) == str(metadata.token_endpoint)
    assert str(loaded.authorization_endpoint) == str(
        metadata.authorization_endpoint
    )
    assert str(loaded.issuer) == str(metadata.issuer)


# ── _persist_discovered_oauth_metadata side-effect tests ──────────
# The two call sites (initial-consent ``_acquire_tokens`` success
# path, and ``reconnect_with_stored_tokens`` success path) both just
# delegate to this helper. Pin the helper's behavior; a bug in the
# call sites would surface as a missing ``oauth_metadata.persisted``
# log in prod (now also covered by the structlog capture below).


@pytest.mark.asyncio
async def test_persist_discovered_oauth_metadata_writes_to_storage(
    tmp_path: Path,
) -> None:
    """When the SDK discovers metadata during a live OAuth flow, the
    helper must round-trip it through storage so the next process
    boot's ``_build_oauth_provider`` finds it. Without this, the §3.8
    fix only protects upstreams whose admin manually re-consents."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    metadata = make_oauth_metadata()

    # Build a provider as production does, then simulate the SDK
    # discovering metadata mid-flow.
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )
    provider.context.oauth_metadata = metadata

    await _persist_discovered_oauth_metadata(provider, storage)

    loaded = await storage.get_oauth_metadata()
    assert loaded is not None
    assert str(loaded.token_endpoint) == str(metadata.token_endpoint)


@pytest.mark.asyncio
async def test_persist_discovered_oauth_metadata_noop_when_unset(
    tmp_path: Path,
) -> None:
    """If the SDK never populated ``context.oauth_metadata`` (e.g. an
    upstream whose discovery hasn't happened yet because the periodic
    refresh succeeded against an already-warm cache), the helper must
    be a no-op rather than writing a partial / empty row."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )
    # context.oauth_metadata defaults to None — no manual mutation.

    await _persist_discovered_oauth_metadata(provider, storage)

    assert await storage.get_oauth_metadata() is None


@pytest.mark.asyncio
async def test_persist_discovered_oauth_metadata_emits_persisted_event(
    tmp_path: Path,
) -> None:
    """Operability guard: the ``upstream.oauth.metadata.persisted`` log
    line is the only signal that the §3.8 lazy-population path
    actually fired for legacy rows. If a future refactor inlines the
    helper without re-emitting the event, this test catches it."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    metadata = make_oauth_metadata()
    provider = await _build_oauth_provider(
        _make_upstream(), storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )
    provider.context.oauth_metadata = metadata

    import structlog
    with structlog.testing.capture_logs() as logs:
        await _persist_discovered_oauth_metadata(provider, storage)

    matching = [
        e for e in logs
        if e.get("event") == "upstream.oauth.metadata.persisted"
        and e.get("upstream_id") == UPSTREAM_ID
        and e.get("user") == USER_ID
        and e.get("token_endpoint") == str(metadata.token_endpoint)
    ]
    assert matching, f"expected oauth.metadata.persisted event, got: {logs}"


@pytest.mark.asyncio
async def test_build_oauth_provider_emits_metadata_hit_event(
    tmp_path: Path,
) -> None:
    """Smoking-gun field for §3.8 triage: a refresh 404 plus a
    preceding ``oauth.metadata.hit`` whose ``token_endpoint`` field
    points to the wrong URL would mean the persisted row is stale
    (likely the upstream rotated its endpoint). vs.
    ``oauth.metadata.miss`` plus a fallback POST to ``<base>/token``
    means the row is just not there yet — different fix."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    metadata = make_oauth_metadata()
    await storage.set_oauth_metadata(metadata)

    import structlog
    with structlog.testing.capture_logs() as logs:
        await _build_oauth_provider(
            _make_upstream(), storage,
            _noop_redirect, _noop_callback,
            SERVER_URL,
        )

    hits = [
        e for e in logs
        if e.get("event") == "upstream.oauth.metadata.hit"
        and e.get("token_endpoint") == str(metadata.token_endpoint)
    ]
    assert hits, f"expected oauth.metadata.hit event, got: {logs}"


@pytest.mark.asyncio
async def test_build_oauth_provider_emits_metadata_miss_event(
    tmp_path: Path,
) -> None:
    """Mirror of the hit case — without persisted metadata, the SDK
    will fall back to ``<base>/token``. The ``miss`` event tells
    operators which upstream is in the pre-fix state."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)

    import structlog
    with structlog.testing.capture_logs() as logs:
        await _build_oauth_provider(
            _make_upstream(), storage,
            _noop_redirect, _noop_callback,
            SERVER_URL,
        )

    misses = [
        e for e in logs
        if e.get("event") == "upstream.oauth.metadata.miss"
        and e.get("upstream_id") == UPSTREAM_ID
    ]
    assert misses, f"expected oauth.metadata.miss event, got: {logs}"


@pytest.mark.asyncio
async def test_corrupt_oauth_metadata_row_falls_back_to_none(
    tmp_path: Path,
) -> None:
    """Schema drift defense: an SDK upgrade adds a required field to
    ``OAuthMetadata``, or a corrupt row is on disk. ``get_oauth_metadata``
    must log + return None rather than crashing the connect path. The
    SDK then falls back to the `<base>/token` path — one cycle of
    degraded behavior, then 401-recovery overwrites the bad row."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    # Bypass the typed setter and write garbage directly.
    await store.put_oauth_metadata(
        DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID,
        {"issuer": "not-a-url", "this_is": "not valid OAuthMetadata"},
    )

    import structlog
    with structlog.testing.capture_logs() as logs:
        result = await storage.get_oauth_metadata()

    assert result is None
    warnings = [
        e for e in logs
        if e.get("event") == "upstream.oauth.metadata.deserialize.failed"
    ]
    assert warnings, (
        f"expected deserialize.failed warning, got: {logs}"
    )
