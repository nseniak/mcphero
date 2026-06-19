"""``validate_startup_secrets`` sandbox-provider matrix (CFG-1).

The cloud-mode startup validator is the gate that keeps a misapplied
env var from booting the backend into an unsafe or dead sandbox
configuration:

- ``own-runner`` — the deleted legacy backend; a stale env var must
  not silently disable sandboxing.
- an unknown provider string — typo / future value; fail loudly.
- ``e2b`` without an API key — the SDK can't authenticate; fail before
  the first session instead of hours later.
- ``local-subprocess`` — the no-isolation dev path; rejected outright
  in cloud so stdio MCPs never run unsandboxed on the prod host.
- ``e2b`` + key — the one accepted cloud configuration.

Standalone mode validates none of this (the app runs on the user's own
machine), so every provider value is accepted there.

Builders are explicit per project convention; cloud-mode runs need the
other required secrets present so the matrix branch is actually reached
(it sits after the missing-secrets check).
"""
from __future__ import annotations

import pytest

from mcpolis.entrypoints.config import (
    Settings,
    StartupConfigError,
    validate_startup_secrets,
)


def make_cloud_settings(
    *, sandbox_provider: str, e2b_api_key: str = "",
) -> Settings:
    """Cloud-mode Settings with every non-sandbox required secret set,
    so ``validate_startup_secrets`` reaches the sandbox-provider matrix
    rather than tripping the missing-secrets gate first."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        oauth_provider="google",
        google_client_id="gci-test",
        session_secret="a-real-non-dev-session-secret",
        encryption_key="a-real-encryption-key",
        mongo_uri="mongodb://mongo:27017",
        redis_url="redis://redis:6379",
        sandbox_provider=sandbox_provider,
        e2b_api_key=e2b_api_key,
    )


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


def test_cloud_local_subprocess_rejected() -> None:
    settings = make_cloud_settings(sandbox_provider="local-subprocess")
    with pytest.raises(StartupConfigError) as exc:
        validate_startup_secrets(settings)
    assert "local-subprocess" in str(exc.value)


# ---------- cloud: accepted ----------


def test_cloud_e2b_with_key_ok() -> None:
    settings = make_cloud_settings(
        sandbox_provider="e2b", e2b_api_key="e2b_real_key",
    )
    # No raise == accepted.
    validate_startup_secrets(settings)


def test_cloud_empty_provider_ok() -> None:
    """Empty ``MCPOLIS_SANDBOX_PROVIDER`` skips the matrix entirely —
    the empty-value fallback (e2b-when-key-else-local) is resolved
    later in ``_build_sandbox_provider_plumbing``, not gated here."""
    settings = make_cloud_settings(sandbox_provider="", e2b_api_key="")
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
