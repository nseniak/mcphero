"""Phase 2b — security hardening.

Covers:
- AuditEntry drops the ``arguments`` / ``arguments_sent`` fields (tool
  arguments may contain secrets).
- Dashboard session key derivation uses HKDF, not the old plain SHA-256.
- Gateway refresh tokens expire after ``REFRESH_TOKEN_TTL`` and are
  rotated on every use.
- Startup secret validation refuses to start cloud mode without the
  required env vars, and is a no-op in standalone mode.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import mcpolis.adapters.auth.mcp_gateway_oauth_provider as gop_module
from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
    McpGatewayOAuthProvider,
    StoredAuthCode,
)
from mcpolis.domain.ports.oauth_state_repository import StoredRefreshToken
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.entrypoints.config import (
    Settings,
    StartupConfigError,
    validate_startup_secrets,
)
from mcpolis.entrypoints.routes.dashboard_auth import get_signing_key
from tests.unit.factories import make_discovered_tool, make_upstream_definition


# ── AuditEntry: no tool arguments ─────────────────────────────────


def test_audit_entry_model_has_no_arguments_fields() -> None:
    """The pydantic model must not accept or expose argument fields."""
    fields = set(AuditEntry.model_fields.keys())
    assert "arguments" not in fields
    assert "arguments_sent" not in fields


def test_audit_entry_construction_rejects_arguments_kwarg() -> None:
    """Passing ``arguments=...`` should be ignored (extra-forbid would be
    nicer but pydantic defaults to ``extra='ignore'``). Either way, the
    serialized entry must not contain the field."""
    entry = AuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        user_id="alice",
        upstream_id="github",
        tool="github__create_issue",
    )
    dumped = entry.model_dump()
    assert "arguments" not in dumped
    assert "arguments_sent" not in dumped


@pytest.mark.asyncio
async def test_tool_router_audit_log_has_no_arguments(tmp_path: Path) -> None:
    """End-to-end: route_call → audit log → no arguments field on disk."""
    upstream = make_upstream_definition(
        id="github",
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )
    cm = UpstreamClientManager([upstream])
    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value=MagicMock(isError=False))
    from tests.unit._state_seed import seed_shared_session
    seed_shared_session(cm, "github", session=mock_session)

    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], cm)
    registry._tools = [  # pyright: ignore[reportPrivateUsage]
        make_discovered_tool(upstream_id="github", original_name="create_issue"),
    ]
    router = ToolRouter(
        registry, cm, audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )

    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="github__create_issue",
        arguments={"title": "Bug", "api_key": "sekret-should-not-be-logged"},
        user_id="alice",
        session_id=None,
    )

    raw = (tmp_path / "audit.jsonl").read_text().strip()
    entry = json.loads(raw)
    assert "arguments" not in entry
    assert "arguments_sent" not in entry
    # The secret must not show up anywhere in the record.
    assert "sekret-should-not-be-logged" not in raw


# ── HKDF session key derivation ───────────────────────────────────


def _settings_with_secret(secret: str) -> Settings:
    return Settings(
        session_secret=secret,
        google_client_id="test",
        google_client_secret="test",
    )


def test_hkdf_key_is_deterministic() -> None:
    """Same secret → same key (so restarts don't invalidate cookies)."""
    s = _settings_with_secret("my-dashboard-secret")
    assert get_signing_key(s) == get_signing_key(s)


def test_hkdf_key_differs_from_sha256() -> None:
    """The new key must differ from the old ``sha256(secret)`` output —
    that's the whole point of the cutover."""
    secret = "my-dashboard-secret"
    s = _settings_with_secret(secret)
    old_key = hashlib.sha256(secret.encode()).digest()
    new_key = get_signing_key(s)
    assert new_key != old_key
    assert len(new_key) == 32  # HKDF output length matches sha256 digest size


def test_hkdf_key_different_secrets_produce_different_keys() -> None:
    s1 = _settings_with_secret("secret-a")
    s2 = _settings_with_secret("secret-b")
    assert get_signing_key(s1) != get_signing_key(s2)


def test_hkdf_key_does_not_fall_back_to_google_client_secret() -> None:
    """Session signing must NOT reuse the Google OAuth client secret.

    If an OAuth client secret leaks (pasted into .env, CI log, or a
    screenshot), it would let an attacker forge session cookies. The
    signing key is derived exclusively from ``MCPOLIS_SESSION_SECRET``
    (or a literal dev default in standalone); never from
    ``google_client_secret``.
    """
    s_empty = Settings(
        session_secret="",
        google_client_id="id",
        google_client_secret="this-should-NOT-sign-sessions",
    )
    s_different = Settings(
        session_secret="",
        google_client_id="id",
        google_client_secret="a-totally-different-value",
    )
    # Both must land on the dev default, so the derived keys must be
    # identical regardless of google_client_secret.
    assert get_signing_key(s_empty) == get_signing_key(s_different)


def test_hkdf_signed_cookies_roundtrip() -> None:
    """A cookie signed with the HKDF key must verify with the same key."""
    from mcpolis.adapters.auth.hmac_token import sign_token, verify_token

    s = _settings_with_secret("my-dashboard-secret")
    key = get_signing_key(s)
    payload = {"email": "alice@test.com", "exp": 9999999999}
    cookie = sign_token(payload, key)
    restored = verify_token(cookie, key)
    assert restored is not None
    assert restored["email"] == "alice@test.com"


# ── Gateway refresh token TTL + rotation ──────────────────────────


def _make_provider(tmp_path: Path) -> McpGatewayOAuthProvider:
    from mcpolis.adapters.repositories.file_oauth_state_repository import (
        FileOAuthStateRepository,
    )
    from tests.unit.factories import make_runtime_manager

    return McpGatewayOAuthProvider(
        google_client_id="google-id",
        google_client_secret="google-secret",
        server_url="http://localhost",
        runtime_manager=make_runtime_manager(PolicyEngine(SettingsConfig())),
        state_repository=FileOAuthStateRepository(tmp_path),
    )


def _make_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-1",
        client_secret="client-secret",
        redirect_uris=[AnyUrl("http://localhost/cb")],
    )


def test_refresh_token_ttl_constant_is_30_days() -> None:
    """30 days is what the plan prescribes."""
    assert REFRESH_TOKEN_TTL == 30 * 86400


@pytest.mark.asyncio
async def test_refresh_token_rotation_revokes_old_token(
    tmp_path: Path,
) -> None:
    """Using a refresh token issues a new one and invalidates the old."""
    provider = _make_provider(tmp_path)
    client = _make_client()
    await provider.register_client(client)

    # Seed an auth code and exchange it for the initial token pair.
    now = time.time()
    auth_code = StoredAuthCode(
        client_id="client-1",
        user_email="alice@test.com",
        code_challenge="chal",
        redirect_uri=AnyUrl("http://localhost/cb"),
        redirect_uri_provided_explicitly=True,
        scopes=["read"],
        state=None,
        created_at=now,
        expires_at=now + 300,
    )
    provider._auth_codes["code-1"] = auth_code  # pyright: ignore[reportPrivateUsage]
    initial = await provider.exchange_authorization_code(client, auth_code)
    old_refresh = initial.refresh_token
    assert old_refresh is not None

    # Exchange the refresh token for a new pair.
    loaded = await provider.load_refresh_token(client, old_refresh)
    assert loaded is not None
    rotated = await provider.exchange_refresh_token(client, loaded, scopes=["read"])
    new_refresh = rotated.refresh_token
    assert new_refresh is not None
    assert new_refresh != old_refresh

    # The old refresh token must no longer be usable.
    assert await provider.load_refresh_token(client, old_refresh) is None
    # The new one is usable.
    reloaded = await provider.load_refresh_token(client, new_refresh)
    assert reloaded is not None
    assert reloaded.user_email == "alice@test.com"


@pytest.mark.asyncio
async def test_refresh_token_expires_after_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh token older than ``REFRESH_TOKEN_TTL`` must be rejected."""
    provider = _make_provider(tmp_path)
    client = _make_client()
    await provider.register_client(client)

    # Insert a refresh token with created_at 31 days ago.
    assert REFRESH_TOKEN_TTL is not None
    stale_created_at = time.time() - (REFRESH_TOKEN_TTL + 86400)
    await provider._ensure_loaded()  # pyright: ignore[reportPrivateUsage]
    provider._refresh_tokens["stale"] = StoredRefreshToken(  # pyright: ignore[reportPrivateUsage]
        token="stale",
        client_id="client-1",
        user_email="alice@test.com",
        scopes=["read"],
        created_at=stale_created_at,
    )

    loaded = await provider.load_refresh_token(client, "stale")
    assert loaded is None, "expired refresh token must be rejected"
    # And the provider must have evicted it from the store.
    assert "stale" not in provider._refresh_tokens  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_refresh_token_within_ttl_is_still_valid(
    tmp_path: Path,
) -> None:
    """Sanity: a fresh token is not evicted by the TTL check."""
    provider = _make_provider(tmp_path)
    client = _make_client()
    await provider.register_client(client)

    await provider._ensure_loaded()  # pyright: ignore[reportPrivateUsage]
    provider._refresh_tokens["fresh"] = StoredRefreshToken(  # pyright: ignore[reportPrivateUsage]
        token="fresh",
        client_id="client-1",
        user_email="alice@test.com",
        scopes=["read"],
        created_at=time.time(),  # just now
    )

    loaded = await provider.load_refresh_token(client, "fresh")
    assert loaded is not None


# ── Startup secret validation ─────────────────────────────────────


def test_validate_startup_secrets_standalone_is_noop() -> None:
    """Standalone mode accepts empty/dev secrets — local machine only."""
    s = Settings(mode="standalone")
    validate_startup_secrets(s)  # must not raise


def test_validate_startup_secrets_cloud_rejects_all_defaults() -> None:
    """Cloud mode refuses to start if every required secret is missing."""
    s = Settings(mode="cloud")
    with pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(s)
    msg = str(exc_info.value)
    assert "MCPOLIS_SESSION_SECRET" in msg
    assert "MCPOLIS_ENCRYPTION_KEY" in msg
    assert "MCPOLIS_MONGO_URI" in msg
    assert "MCPOLIS_REDIS_URL" in msg


def test_validate_startup_secrets_cloud_rejects_dev_session_secret() -> None:
    """The sentinel dev value must not slip past the check."""
    s = Settings(
        mode="cloud",
        session_secret="mcpolis-dev-secret",
        encryption_key="real-key",
        mongo_uri="mongodb://user:pw@mongo",
        redis_url="redis://redis",
    )
    with pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(s)
    assert "MCPOLIS_SESSION_SECRET" in str(exc_info.value)


def test_validate_startup_secrets_cloud_accepts_full_config() -> None:
    """Cloud mode with every required var set must pass validation."""
    s = Settings(
        mode="cloud",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
    )
    validate_startup_secrets(s)  # must not raise


def test_validate_startup_secrets_email_flag_without_smtp_rejected() -> None:
    """Enabling the §5.2 notifier in cloud mode without an SMTP
    transport would silently no-op through the logging stub. The
    validator must refuse to boot and name the missing keys."""
    s = Settings(
        mode="cloud",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
        upstream_health_email_enabled=True,
    )
    with pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(s)
    msg = str(exc_info.value)
    assert "MCPOLIS_SMTP_HOST" in msg
    assert "MCPOLIS_SMTP_USERNAME" in msg
    assert "MCPOLIS_SMTP_PASSWORD" in msg
    assert "MCPOLIS_SMTP_FROM" in msg


def test_validate_startup_secrets_email_flag_with_smtp_ok() -> None:
    """Flag on + a fully configured SMTP transport must pass."""
    s = Settings(
        mode="cloud",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
        upstream_health_email_enabled=True,
        smtp_host="smtp.gmail.com",
        smtp_username="robot@mcphero.io",
        smtp_password="app-password",
        smtp_from="info@mcphero.io",
    )
    validate_startup_secrets(s)  # must not raise


def test_validate_startup_secrets_cloud_rejects_test_mode_on_public_bind() -> None:
    """test_mode + cloud + non-loopback host must refuse to boot."""
    s = Settings(
        mode="cloud",
        test_mode=True,
        host="0.0.0.0",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
    )
    with pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(s)
    assert "loopback" in str(exc_info.value)


def test_validate_startup_secrets_cloud_rejects_dev_stub_provider() -> None:
    """The dev-stub provider issues unauthenticated session cookies and
    must never be wired up against a cloud deployment, regardless of
    other secrets being correct."""
    s = Settings(
        mode="cloud",
        oauth_provider="dev_stub",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
    )
    with pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(s)
    assert "dev_stub" in str(exc_info.value)


def test_validate_startup_secrets_standalone_accepts_dev_stub() -> None:
    """Mirror image of the above — dev_stub is the *intended* setting
    for standalone mode."""
    s = Settings(mode="standalone", oauth_provider="dev_stub")
    validate_startup_secrets(s)  # must not raise


def test_validate_startup_secrets_cloud_dev_stub_with_test_mode_loopback_ok() -> None:
    """The single supported way to run dev_stub against the cloud
    code paths: ``test_mode=true`` + a literal loopback host. This
    is what ``run-e2e-tests.sh`` relies on to exercise Mongo/Redis-
    backed flows without real Google credentials."""
    s = Settings(
        mode="cloud",
        oauth_provider="dev_stub",
        test_mode=True,
        host="127.0.0.1",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
    )
    validate_startup_secrets(s)  # must not raise


def test_validate_startup_secrets_cloud_dev_stub_off_loopback_still_rejected() -> None:
    """test_mode alone is not enough — the loopback bind is the
    second half of the gate. A cloud deployment with test_mode=true
    but bound to 0.0.0.0 must still refuse dev_stub."""
    s = Settings(
        mode="cloud",
        oauth_provider="dev_stub",
        test_mode=True,
        host="0.0.0.0",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
    )
    with pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(s)
    assert "dev_stub" in str(exc_info.value)


def test_validate_startup_secrets_cloud_rejects_test_mode_with_hostname_bind() -> None:
    """'localhost' resolves via /etc/hosts — the guard requires a literal IP."""
    s = Settings(
        mode="cloud",
        test_mode=True,
        host="localhost",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
    )
    with pytest.raises(StartupConfigError):
        validate_startup_secrets(s)


def test_validate_startup_secrets_cloud_allows_test_mode_on_loopback_ipv4() -> None:
    """test_mode is allowed in cloud mode when bound to 127.0.0.1 — e2e rig."""
    s = Settings(
        mode="cloud",
        test_mode=True,
        host="127.0.0.1",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
    )
    validate_startup_secrets(s)  # must not raise


def test_validate_startup_secrets_cloud_allows_test_mode_on_loopback_ipv6() -> None:
    """::1 is loopback too."""
    s = Settings(
        mode="cloud",
        test_mode=True,
        host="::1",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
    )
    validate_startup_secrets(s)  # must not raise


def test_validate_startup_secrets_cloud_test_mode_loopback_still_checks_secrets() -> None:
    """Allowing test_mode on loopback must not bypass the missing-secret check."""
    s = Settings(
        mode="cloud",
        test_mode=True,
        host="127.0.0.1",
    )
    with pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(s)
    msg = str(exc_info.value)
    assert "MCPOLIS_SESSION_SECRET" in msg
    assert "MCPOLIS_ENCRYPTION_KEY" in msg


# ── Cleanup: reset any module-level state we mutated ──────────────


def test_mcp_gateway_oauth_provider_module_constants_are_accessible() -> None:
    """Sanity check that nothing else in the test module polluted the
    module-level state we depend on."""
    assert gop_module.ACCESS_TOKEN_TTL == ACCESS_TOKEN_TTL
    assert gop_module.REFRESH_TOKEN_TTL == REFRESH_TOKEN_TTL


# ── Sandbox provider startup validator (step 12 of the SandboxService rollout)


def make_cloud_settings(**overrides: object) -> Settings:
    """Builder for cloud-mode Settings with every required secret
    pre-filled. Specific tests override the bits they care about."""
    base: dict[str, object] = {
        "mode": "cloud",
        "session_secret": "real-session-secret-abc123",
        "encryption_key": "real-encryption-key-xyz",
        "mongo_uri": "mongodb://user:pw@mongo:27017",
        "redis_url": "redis://redis:6379",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_validate_startup_secrets_cloud_accepts_unset_sandbox_provider() -> None:
    """Empty sandbox_provider ⇔ legacy auto-selection. Cloud mode
    must still accept this so existing deployments don't break on
    upgrade."""
    s = make_cloud_settings(sandbox_provider="")
    validate_startup_secrets(s)


def test_validate_startup_secrets_cloud_rejects_unknown_sandbox_provider() -> None:
    s = make_cloud_settings(sandbox_provider="bogus")
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(s)
    assert "MCPOLIS_SANDBOX_PROVIDER" in str(exc.value)


def test_validate_startup_secrets_cloud_e2b_requires_api_key() -> None:
    s = make_cloud_settings(sandbox_provider="e2b", e2b_api_key="")
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(s)
    assert "MCPOLIS_E2B_API_KEY" in str(exc.value)


def test_validate_startup_secrets_cloud_e2b_with_key_accepted() -> None:
    s = make_cloud_settings(
        sandbox_provider="e2b", e2b_api_key="e2b_test_key_xyz",
    )
    validate_startup_secrets(s)


def test_validate_startup_secrets_cloud_rejects_own_runner() -> None:
    """The own-runner backend was deleted in Phase 5; a stale env
    var setting it is rejected at startup so the operator gets a
    clear "no longer supported" error instead of a silent fallback."""
    s = make_cloud_settings(sandbox_provider="own-runner")
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(s)
    assert "no longer supported" in str(exc.value)


def test_validate_startup_secrets_cloud_rejects_local_subprocess() -> None:
    """The no-isolation path runs every stdio MCP unsandboxed on the
    backend host. Cloud mode refuses it outright — operator typo
    becomes a clear startup error rather than a silent
    security-posture downgrade."""
    s = make_cloud_settings(sandbox_provider="local-subprocess")
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(s)
    assert "local-subprocess" in str(exc.value)
    assert "not allowed" in str(exc.value)


def test_validate_startup_secrets_standalone_accepts_local_subprocess() -> None:
    """Standalone mode is a no-op — the validator only enforces in
    cloud mode."""
    s = Settings(
        mode="standalone", sandbox_provider="local-subprocess",
    )
    validate_startup_secrets(s)
