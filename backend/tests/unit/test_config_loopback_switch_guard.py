"""The loopback test switch is refused on a server with a public listen address.

``MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK=1`` lets admins register MCP URLs on
127.0.0.0/8 and ``::1``, which the SSRF deny-list otherwise refuses. Dev and
e2e runs need it for their local fixture MCPs. On a server that listens on a
public address it would let an org admin reach the host's own local
services, so startup (``validate_startup_secrets``) refuses the combination,
in cloud mode and in standalone mode alike. Only a literal loopback IP counts as a loopback listen address.

The guard reads the switch straight from the process environment, so the
tests set it with ``make_switch_env``: that is the guard's real input, not a
patched object.
"""
from __future__ import annotations

import os
from contextlib import AbstractContextManager
from unittest.mock import patch

import pytest

from mcpolis.domain.services.url_safety import (
    UnsafeUpstreamUrl,
    validate_upstream_url,
)
from mcpolis.entrypoints.config import (
    Settings,
    StartupConfigError,
    validate_startup_secrets,
)

LOOPBACK_SWITCH = "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK"
LOOPBACK_MCP_URL = "http://127.0.0.1:9000/mcp"


def make_cloud_settings(host: str) -> Settings:
    """Cloud settings that pass every other startup check, so the listen
    address and the switch are the only things a test varies.

    ``_env_file=None`` plus explicit values keep a developer's local
    ``.env`` / ``MCPOLIS_*`` variables out of the result: without an
    explicit Google client id, cloud mode stops on the Google check before
    it ever reaches the loopback guard.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        host=host,
        oauth_provider="google",
        google_client_id="test-google-client-id",
        session_secret="real-session-secret-abc123",
        encryption_key="real-encryption-key-xyz",
        mongo_uri="mongodb://user:pw@mongo:27017",
        redis_url="redis://redis:6379",
        test_mode=False,
        sandbox_provider="",
        e2b_api_key="test-e2b-api-key",
        upstream_health_email_enabled=False,
    )


def make_standalone_settings(host: str) -> Settings:
    """Standalone settings with only the listen address chosen; the
    dev-stub sign-in is off so the loopback guard is the one check that
    can refuse."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="standalone",
        host=host,
        oauth_provider="google",
        test_mode=False,
    )


def make_switch_env(value: str | None) -> AbstractContextManager[object]:
    """Process environment with the switch set to ``value``, or unset when
    ``value`` is ``None``. Restores the real environment on exit."""
    env = {k: v for k, v in os.environ.items() if k != LOOPBACK_SWITCH}
    if value is not None:
        env[LOOPBACK_SWITCH] = value
    return patch.dict(os.environ, env, clear=True)


def startup_is_refused(settings: Settings) -> bool:
    try:
        validate_startup_secrets(settings)
    except StartupConfigError:
        return True
    return False


def loopback_mcp_urls_are_accepted() -> bool:
    try:
        validate_upstream_url(LOOPBACK_MCP_URL)
    except UnsafeUpstreamUrl:
        return False
    return True


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "203.0.113.7", "localhost"],
    ids=["all-ipv4-interfaces", "all-ipv6-interfaces", "public-ip", "hostname"],
)
def test_switch_on_without_a_loopback_listen_address_refuses_to_start(
    host: str,
) -> None:
    """Switch on + a listen address that is not a literal loopback IP
    (all interfaces, a public IP, or even the name ``localhost``): the
    cloud server refuses to start and the error names the switch and the
    address."""
    with make_switch_env("1"), pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(make_cloud_settings(host=host))

    message = str(exc_info.value)
    assert LOOPBACK_SWITCH in message
    assert repr(host) in message


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"], ids=["ipv4", "ipv6"])
def test_switch_on_with_a_loopback_listen_address_starts(host: str) -> None:
    """Switch on + a loopback listen address is the local cloud-mode e2e
    setup: the server starts."""
    with make_switch_env("1"):
        validate_startup_secrets(make_cloud_settings(host=host))  # must not raise


def test_public_listen_address_without_the_switch_starts() -> None:
    """A production deploy listens on all interfaces and never sets the
    switch: the server starts."""
    with make_switch_env(None):
        validate_startup_secrets(make_cloud_settings(host="0.0.0.0"))  # must not raise


@pytest.mark.parametrize("switch_value", ["1", "true", "0", ""])
def test_public_listen_address_is_refused_exactly_when_loopback_urls_open(
    switch_value: str,
) -> None:
    """The startup guard and the MCP URL check read the switch the same
    way: every switch value that lets admins register a loopback MCP URL
    also makes a server on a public listen address refuse to start, and
    no other value does."""
    with make_switch_env(switch_value):
        assert startup_is_refused(make_cloud_settings(host="0.0.0.0")) == (
            loopback_mcp_urls_are_accepted()
        )


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "203.0.113.7"], ids=["all-interfaces", "public-ip"],
)
def test_standalone_switch_on_without_a_loopback_listen_address_refuses_to_start(
    host: str,
) -> None:
    """A standalone server reachable from other machines is held to the
    same rule as cloud: with the switch on it refuses to start, because an
    org admin could otherwise point an MCP at the host's local services."""
    with make_switch_env("1"), pytest.raises(StartupConfigError) as exc_info:
        validate_startup_secrets(make_standalone_settings(host=host))

    assert LOOPBACK_SWITCH in str(exc_info.value)


def test_standalone_switch_on_with_a_loopback_listen_address_starts() -> None:
    """``bash start.sh standalone`` (switch on, 127.0.0.1) still starts."""
    with make_switch_env("1"):
        validate_startup_secrets(make_standalone_settings(host="127.0.0.1"))


def test_standalone_public_listen_address_without_the_switch_starts() -> None:
    """A standalone server on all interfaces without the switch starts."""
    with make_switch_env(None):
        validate_startup_secrets(make_standalone_settings(host="0.0.0.0"))
