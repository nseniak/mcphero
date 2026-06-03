"""Unit tests for ``DevStubOAuthProvider``.

Covers the provider's own contract: URL building, code → email
exchange, and the cloud-mode refusal. End-to-end coverage that mounts
the picker route, redirects through the dashboard callback, and
verifies a real signed cookie comes back lives in
``test_dashboard_dev_stub_login.py``.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from mcpolis.adapters.auth.dev_stub_oauth_provider import (
    DevStubOAuthProvider,
)


def make_provider() -> DevStubOAuthProvider:
    return DevStubOAuthProvider(mode="standalone")


def test_provider_refuses_to_instantiate_in_cloud_mode() -> None:
    """Defense in depth — startup validation in config.py also blocks
    this, but a programming mistake (missed branch, wrong factory
    call) shouldn't be enough to wire up the stub against a cloud
    deploy."""
    with pytest.raises(RuntimeError, match="cloud mode"):
        DevStubOAuthProvider(mode="cloud")


def test_provider_can_be_instantiated_in_cloud_mode_with_explicit_opt_in() -> None:
    """``allow_in_cloud=True`` is the supported escape hatch for the
    cloud-mode e2e harness (``run-e2e-tests.sh`` runs cloud +
    test_mode + loopback). The startup validator is the primary
    gate; this constructor flag mirrors it so accidental misuse
    still fails loud, while the e2e wiring stays a one-liner."""
    DevStubOAuthProvider(mode="cloud", allow_in_cloud=True)  # must not raise


def test_provider_name_is_dev_stub() -> None:
    """Surfaces in analytics + logs; tests Phase D will assert this is
    what the cookie's ``auth_method`` records."""
    assert make_provider().name == "dev_stub"


@pytest.mark.asyncio
async def test_start_login_redirects_to_in_app_picker() -> None:
    provider = make_provider()
    url = await provider.start_login(
        state="state-1",
        redirect_uri="https://dev.example.com/api/auth/callback",
        join=None,
    )

    parsed = urlparse(url)
    assert parsed.path == "/api/auth/dev-stub/picker"
    qs = parse_qs(parsed.query)
    assert qs["state"] == ["state-1"]
    assert qs["redirect_uri"] == [
        "https://dev.example.com/api/auth/callback",
    ]
    assert "join" not in qs


@pytest.mark.asyncio
async def test_start_login_propagates_join_when_provided() -> None:
    provider = make_provider()
    url = await provider.start_login(
        state="s",
        redirect_uri="http://localhost/cb",
        join="acme",
    )
    qs = parse_qs(urlparse(url).query)
    assert qs["join"] == ["acme"]


@pytest.mark.asyncio
async def test_complete_login_treats_code_as_email() -> None:
    """The picker ↔ submit ↔ callback round-trip plumbs the chosen
    email through as the ``code`` query param. ``complete_login`` is
    just a typed unwrap — no token exchange happens."""
    provider = make_provider()
    completed = await provider.complete_login(
        code="alice@example.test",
        state="s",
        redirect_uri="http://localhost/cb",
    )
    assert completed.email == "alice@example.test"
    assert completed.raw_id_token is None


@pytest.mark.asyncio
async def test_complete_login_rejects_non_email_code() -> None:
    """If somebody hits ``/callback`` directly with a non-email
    ``code``, we 400 instead of silently issuing a cookie under a
    garbage username."""
    provider = make_provider()
    with pytest.raises(ValueError, match="email-shaped"):
        await provider.complete_login(
            code="not-an-email",
            state="s",
            redirect_uri="http://localhost/cb",
        )


@pytest.mark.asyncio
async def test_complete_login_rejects_empty_code() -> None:
    provider = make_provider()
    with pytest.raises(ValueError):
        await provider.complete_login(
            code="",
            state="s",
            redirect_uri="http://localhost/cb",
        )
