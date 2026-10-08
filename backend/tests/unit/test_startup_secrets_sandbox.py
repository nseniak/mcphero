"""``validate_startup_secrets`` sandbox-provider matrix (CFG-1).

The cloud-mode startup validator is the gate that keeps a misapplied
env var from booting the backend into an unsafe or dead sandbox
configuration:

- ``own-runner`` — the deleted legacy backend; a stale env var must
  not silently disable sandboxing.
- an unknown provider string — typo / future value; fail loudly.
- ``e2b`` without an API key — the SDK can't authenticate; fail before
  the first session instead of hours later.
- ``local-subprocess`` — the no-isolation dev path; rejected in cloud
  unless named explicitly on a literal loopback bind (local dev, e2e),
  so stdio MCPs never run unsandboxed on the prod host.
- empty provider without a key — would silently fall back to
  ``local-subprocess``; rejected in cloud on every bind, loopback
  included (operator decision: no silent fallback in cloud).
- ``e2b`` + key, or empty + key — the accepted production
  configurations.

Standalone mode validates none of this (the app runs on the user's own
machine), so every provider value is accepted there.

Builders are explicit per project convention; cloud-mode runs need the
other required secrets present so the matrix branch is actually reached
(it sits after the missing-secrets check).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import (
    Settings,
    StartupConfigError,
    validate_startup_secrets,
)


# The backend's dev template, which ``bash start.sh`` copies into
# place on a fresh checkout.
DEV_CLOUD_TEMPLATE = Path(__file__).resolve().parents[2] / ".env.cloud.example"

PROD_BIND = "0.0.0.0"
LOOPBACK_BIND = "127.0.0.1"


def make_cloud_settings(
    *, sandbox_provider: str, e2b_api_key: str = "", host: str = LOOPBACK_BIND,
) -> Settings:
    """Cloud-mode Settings with every non-sandbox required secret set,
    so ``validate_startup_secrets`` reaches the sandbox-provider matrix
    rather than tripping the missing-secrets gate first. ``host``
    defaults to the Settings default (loopback). Tests that pass an
    off-loopback bind call ``allow_off_loopback_bind`` first. Every
    other field the validator reads is pinned, so neither an env file
    nor ``MCPOLIS_*`` variables exported in the shell can change the
    outcome."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        host=host,
        test_mode=False,
        upstream_health_email_enabled=False,
        oauth_provider="google",
        google_client_id="gci-test",
        session_secret="a-real-non-dev-session-secret",
        encryption_key="a-real-encryption-key",
        mongo_uri="mongodb://mongo:27017",
        redis_url="redis://redis:6379",
        sandbox_provider=sandbox_provider,
        e2b_api_key=e2b_api_key,
    )


def allow_off_loopback_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run-unit-tests.sh`` exports the loopback-only SSRF test switch,
    and the validator refuses it on an off-loopback bind before it
    reaches the sandbox checks. Production never sets it."""
    monkeypatch.delenv("MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK", raising=False)


def make_standalone_settings(*, sandbox_provider: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="standalone",
        sandbox_provider=sandbox_provider,
    )


# ---------- cloud: rejected providers ----------


def test_cloud_own_runner_rejected() -> None:
    settings = make_cloud_settings(sandbox_provider="own-runner")
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(settings)
    assert "own-runner" in str(exc.value)


def test_cloud_unknown_provider_rejected() -> None:
    settings = make_cloud_settings(sandbox_provider="bogus")
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(settings)
    assert "bogus" in str(exc.value)


def test_cloud_e2b_without_key_rejected() -> None:
    settings = make_cloud_settings(sandbox_provider="e2b", e2b_api_key="")
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(settings)
    assert "MCPOLIS_E2B_API_KEY" in str(exc.value)


@pytest.mark.parametrize("host", [PROD_BIND, "10.0.0.5", "localhost"])
def test_cloud_local_subprocess_rejected_off_loopback(
    host: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named explicitly but bound off-loopback: rejected. ``localhost``
    is a hostname, not a literal loopback IP, so it counts as
    off-loopback (same rule as the other loopback-only switches)."""
    allow_off_loopback_bind(monkeypatch)
    settings = make_cloud_settings(
        sandbox_provider="local-subprocess", host=host,
    )
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(settings)
    assert "local-subprocess" in str(exc.value)
    assert "literal loopback IP" in str(exc.value)
    assert repr(host) in str(exc.value)


@pytest.mark.parametrize("host", [PROD_BIND, LOOPBACK_BIND])
@pytest.mark.parametrize("provider", ["", "   "])
def test_cloud_empty_provider_without_key_rejected(
    provider: str, host: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider and no key would resolve to the unsandboxed runner.
    Cloud mode refuses it on every bind — loopback included, so local
    dev must name the runner rather than land on it silently. A
    whitespace-only value counts as empty, as it does in the
    plumbing that resolves it."""
    allow_off_loopback_bind(monkeypatch)
    settings = make_cloud_settings(
        sandbox_provider=provider, e2b_api_key="", host=host,
    )
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(settings)
    message = str(exc.value)
    assert "MCPOLIS_E2B_API_KEY" in message
    assert "MCPOLIS_SANDBOX_PROVIDER=local-subprocess" in message


def test_create_app_refuses_cloud_empty_provider_without_key() -> None:
    """The live startup path, not just the validator in isolation:
    ``create_app`` must refuse before it builds the sandbox plumbing,
    where the empty value would otherwise fall back to the
    unsandboxed runner."""
    settings = make_cloud_settings(sandbox_provider="", e2b_api_key="")
    with pytest.raises(StartupConfigError) as exc:
        create_app(settings)
    assert "MCPOLIS_E2B_API_KEY" in str(exc.value)


# ---------- cloud: accepted ----------


def test_cloud_e2b_with_key_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production configuration, on the production bind."""
    allow_off_loopback_bind(monkeypatch)
    settings = make_cloud_settings(
        sandbox_provider="e2b", e2b_api_key="e2b_real_key", host=PROD_BIND,
    )
    # No raise == accepted.
    validate_startup_secrets(settings)


def test_cloud_empty_provider_with_key_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty provider + key resolves to ``e2b`` in
    ``_build_sandbox_provider_plumbing``, so it is accepted."""
    allow_off_loopback_bind(monkeypatch)
    settings = make_cloud_settings(
        sandbox_provider="", e2b_api_key="e2b_real_key", host=PROD_BIND,
    )
    validate_startup_secrets(settings)


@pytest.mark.parametrize("host", [LOOPBACK_BIND, "::1"])
def test_cloud_local_subprocess_ok_on_loopback(host: str) -> None:
    """Named explicitly on a literal loopback bind: the local-dev and
    e2e configuration, accepted."""
    settings = make_cloud_settings(
        sandbox_provider="local-subprocess", host=host,
    )
    validate_startup_secrets(settings)


def test_dev_cloud_template_passes_sandbox_checks() -> None:
    """A fresh ``bash start.sh`` copies the dev template and binds the
    default ``127.0.0.1``. Without an E2B key the template must name
    the runner, or a fresh checkout cannot boot. The template does
    not set dashboard auth (that comes from the developer's own env
    file or ``--fake-auth``), so it is supplied here. The template is
    passed as init values, not as an env file, so ``MCPOLIS_*``
    variables exported in the shell cannot override it."""
    template_values: dict[str, object] = {
        key.removeprefix("MCPOLIS_").lower(): value
        for key, value in dotenv_values(DEV_CLOUD_TEMPLATE).items()
        if key.startswith("MCPOLIS_") and value is not None
    }
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        **{
            **template_values,
            "host": LOOPBACK_BIND,
            "test_mode": False,
            "oauth_provider": "google",
            "google_client_id": "gci-test",
            "e2b_api_key": "",
        },
    )
    assert settings.mode == "cloud"
    assert settings.sandbox_provider == "local-subprocess"
    validate_startup_secrets(settings)


# ---------- standalone: anything goes ----------


@pytest.mark.parametrize(
    "provider",
    ["own-runner", "bogus", "e2b", "local-subprocess", ""],
)
def test_standalone_accepts_any_provider(provider: str) -> None:
    """Standalone mode short-circuits before the sandbox matrix — the
    app runs on the user's own machine, so even the no-isolation and
    legacy values are accepted without raising."""
    settings = make_standalone_settings(sandbox_provider=provider)
    validate_startup_secrets(settings)
