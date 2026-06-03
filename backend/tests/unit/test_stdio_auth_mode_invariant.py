"""Stdio MCPs can only use ``service_account`` auth.

OAuth modes (``admin_oauth`` / ``per_user_oauth``) require an HTTP
endpoint the gateway can drive the OAuth handshake against; stdio
sandboxes can't host the dance (see ``docs/stdio-authent.md`` §D).
The model layer rejects the combo at construction; the YAML/JSON
loader coerces stale rows forward with a warning so a legacy on-disk
config doesn't silently disappear from the listing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcpolis.adapters.repositories.upstream_config_loader import (
    load_merged_config,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
    validate_stdio_uses_service_account,
)


def _make_stdio_definition(auth_mode: AuthMode) -> UpstreamDefinition:
    return UpstreamDefinition(
        id="ok",
        display_name="OK",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(command="npx"),
        http=None,
        auth=UpstreamAuthConfig(mode=auth_mode),
    )


def _make_http_definition(auth_mode: AuthMode) -> UpstreamDefinition:
    return UpstreamDefinition(
        id="ok",
        display_name="OK",
        transport=TransportType.streamable_http,
        stdio=None,
        http=HttpTransportConfig(url="https://example.com/mcp"),
        auth=UpstreamAuthConfig(mode=auth_mode),
    )


def test_helper_accepts_stdio_with_service_account() -> None:
    validate_stdio_uses_service_account(
        TransportType.stdio, AuthMode.service_account,
    )


def test_helper_accepts_http_with_any_auth_mode() -> None:
    for mode in (
        AuthMode.service_account,
        AuthMode.admin_oauth,
        AuthMode.per_user_oauth,
    ):
        validate_stdio_uses_service_account(
            TransportType.streamable_http, mode,
        )


def test_helper_rejects_stdio_with_admin_oauth() -> None:
    with pytest.raises(ValueError, match="service_account"):
        validate_stdio_uses_service_account(
            TransportType.stdio, AuthMode.admin_oauth,
        )


def test_helper_rejects_stdio_with_per_user_oauth() -> None:
    with pytest.raises(ValueError, match="service_account"):
        validate_stdio_uses_service_account(
            TransportType.stdio, AuthMode.per_user_oauth,
        )


def test_model_accepts_stdio_with_service_account() -> None:
    upstream = _make_stdio_definition(AuthMode.service_account)
    assert upstream.auth.mode == AuthMode.service_account


def test_model_accepts_http_with_oauth() -> None:
    upstream = _make_http_definition(AuthMode.per_user_oauth)
    assert upstream.auth.mode == AuthMode.per_user_oauth


def test_model_rejects_stdio_with_admin_oauth() -> None:
    with pytest.raises(ValidationError, match="service_account"):
        _make_stdio_definition(AuthMode.admin_oauth)


def test_model_rejects_stdio_with_per_user_oauth() -> None:
    with pytest.raises(ValidationError, match="service_account"):
        _make_stdio_definition(AuthMode.per_user_oauth)


def test_loader_coerces_stale_stdio_admin_oauth_to_service_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A legacy mcp.json + options.yaml row that pairs stdio with
    ``admin_oauth`` is coerced forward to ``service_account`` so the
    listing doesn't silently lose the upstream when the model
    validator lands. A warning is logged so the operator can spot it.
    """
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "legacy": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-everything"],
            },
        },
    }))
    upstream_options = {"legacy": {"auth_mode": "admin_oauth"}}

    result = load_merged_config(
        mcp_json, upstream_options=upstream_options,
    )

    assert len(result) == 1
    assert result[0].id == "legacy"
    assert result[0].auth.mode == AuthMode.service_account
    # structlog renders to stdout via the project's processor chain;
    # caplog (stdlib) doesn't capture it, so use capsys to confirm
    # the warning fired.
    captured = capsys.readouterr()
    assert "stdio_auth_mode.coerced" in captured.out
    assert "stale_auth_mode=admin_oauth" in captured.out


def test_loader_passes_through_http_with_admin_oauth(
    tmp_path: Path,
) -> None:
    """HTTP + ``admin_oauth`` is a valid combo and must not be coerced."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "github": {"url": "https://mcp.github.com/sse"},
        },
    }))
    upstream_options = {"github": {"auth_mode": "admin_oauth"}}

    result = load_merged_config(
        mcp_json, upstream_options=upstream_options,
    )

    assert len(result) == 1
    assert result[0].auth.mode == AuthMode.admin_oauth
