"""Tests for the defensive secret scanner.

The scanner runs on save and emits ``secret_in_json_detected`` events.
These tests confirm each pattern fires and that benign values don't
trigger false positives. None of the tests assert on log calls — the
scanner is a pure function, the logging is the caller's job.
"""
from __future__ import annotations

from mcpolis.domain.services.secret_scanner import (
    HIDDEN_VALUE,
    hide_secret_args,
    hide_secret_values,
    hide_secrets_in_text,
    scan_for_secrets,
)


def test_scanner_detects_github_token() -> None:
    findings = scan_for_secrets(
        env={"GITHUB_TOKEN": "ghp_abcdefghijklmnop1234567890ABCDEF"},
    )
    assert len(findings) == 1
    assert findings[0].pattern == "github_token"
    assert findings[0].field == "env"
    assert findings[0].key == "GITHUB_TOKEN"
    # Preview is short and never contains the full secret.
    assert "ghp_ab" in findings[0].match_preview
    assert "ghp_abcdefghijklmnop1234567890ABCDEF" not in findings[0].match_preview


def test_scanner_detects_openai_key() -> None:
    findings = scan_for_secrets(
        env={"OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz0123"},
    )
    assert len(findings) == 1
    assert findings[0].pattern == "openai_or_stripe_key"


def test_scanner_detects_aws_access_key() -> None:
    findings = scan_for_secrets(env={"X": "AKIAABCDEFGHIJKLMNOP"})
    assert len(findings) == 1
    assert findings[0].pattern == "aws_access_key"


def test_scanner_detects_google_api_key() -> None:
    # ``AIza`` + exactly 35 chars from ``[A-Za-z0-9_-]``.
    body = "SyA_abcdefghijklmnopqrstuvwxyz01234"
    assert len(body) == 35
    findings = scan_for_secrets(env={"X": f"AIza{body}"})
    assert len(findings) == 1
    assert findings[0].pattern == "google_api_key"


def test_scanner_detects_jwt_in_header() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    findings = scan_for_secrets(headers={"Authorization": f"Bearer {jwt}"})
    assert len(findings) == 1
    assert findings[0].pattern == "jwt"
    assert findings[0].field == "headers"


def test_scanner_high_entropy_fires_on_secret_named_key() -> None:
    findings = scan_for_secrets(
        env={"MY_SECRET": "abcXYZ012!@#$%^&*()abcXYZ012abcXYZ012"},
    )
    assert len(findings) == 1
    assert findings[0].pattern == "high_entropy"


def test_scanner_high_entropy_does_not_fire_on_neutral_key_name() -> None:
    # Same value but the key name is plain — entropy fallback gates
    # on the key-name heuristic so this passes through.
    findings = scan_for_secrets(
        env={"NODE_ENV": "abcXYZ012!@#$%^&*()abcXYZ012abcXYZ012"},
    )
    assert findings == []


def test_scanner_does_not_flag_short_low_entropy_values() -> None:
    findings = scan_for_secrets(
        env={
            "NODE_ENV": "production",
            "LOG_LEVEL": "debug",
            "PORT": "8080",
        },
    )
    assert findings == []


def test_scanner_skips_already_referenced_placeholders() -> None:
    # Even if the key looks suspect, ``${VAR}`` references are the
    # solution, not the problem.
    findings = scan_for_secrets(env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"})
    assert findings == []


def test_scanner_walks_env_and_headers_independently() -> None:
    findings = scan_for_secrets(
        env={"GH": "ghp_abcdefghijklmnop1234567890ABCDEF"},
        headers={"X-Stripe-Key": "sk_live_abcdefghijklmnop12345"},
    )
    assert len(findings) == 2
    assert {f.field for f in findings} == {"env", "headers"}


def test_scanner_handles_none_inputs() -> None:
    assert scan_for_secrets() == []
    assert scan_for_secrets(env=None, headers=None) == []


def test_scanner_detects_stripe_live_key() -> None:
    findings = scan_for_secrets(
        env={"STRIPE_KEY": "sk_live_abcdefghijklmnop1234567890"},
    )
    assert len(findings) == 1
    assert findings[0].pattern == "stripe_live_key"


def test_scanner_detects_stripe_test_key() -> None:
    findings = scan_for_secrets(
        env={"STRIPE_KEY": "rk_test_abcdefghijklmnop1234"},
    )
    assert len(findings) == 1
    assert findings[0].pattern == "stripe_live_key"


def test_scanner_detects_slack_token() -> None:
    findings = scan_for_secrets(
        env={"SLACK_TOKEN": "xoxb-123456789012-abcdefABCDEF"},
    )
    assert len(findings) == 1
    assert findings[0].pattern == "slack_token"


def test_scanner_match_preview_truncates_long_value() -> None:
    long_value = "ghp_" + "x" * 40
    findings = scan_for_secrets(env={"X": long_value})
    assert len(findings) == 1
    preview = findings[0].match_preview
    # 6 chars + ellipsis when source is longer than 6.
    assert preview == "ghp_xx…"
    # Critical: full secret never appears in the preview.
    assert long_value not in preview


def test_scanner_match_preview_passes_through_short_value() -> None:
    # Value is exactly 6 chars: pre-truncation, the preview should be
    # the value verbatim. Not flagged since no provider regex / key
    # heuristic fires.
    assert scan_for_secrets(env={"X": "short6"}) == []
    # A 6-char value with secret-suggestive key + entropy: still
    # below the entropy length threshold (16).
    assert scan_for_secrets(env={"TOKEN": "ABCxyz"}) == []


# --- Hiding credentials from a reader (the Admin MCP's get_upstream) ---
# Errs on hiding: a reader like an AI client must never see one.


def test_hide_secret_values_hides_every_value_but_references() -> None:
    """Deny by default: a credential hides behind harmless-looking names
    and shapes too (a password inside ``DATABASE_URL``, a ``Cookie``, a
    GitLab token under the name ``PAT``), so a plain setting is hidden
    as well."""
    values = {
        "Authorization": "Bearer abc123",
        "X-Custom": "token abc123",
        "DATABASE_URL": "postgres://admin:S3cretPassw0rd@db.example.com:5432/app",
        "Cookie": "session=abcd1234efgh5678ijkl9012",
        "PAT": "glpat-abcdefghij0123456789",
        "AUTH_MODE": "oauth",
        "LOG_LEVEL": "debug",
    }

    assert hide_secret_values(values) == dict.fromkeys(values, HIDDEN_VALUE)


def test_hide_secret_values_shows_values_made_only_of_references() -> None:
    values = {
        "Authorization": "Bearer ${API_TOKEN}",
        "GITHUB_TOKEN": "${GITHUB_TOKEN}",
        "CREDENTIALS": "${USER} ${PASSWORD}",
        "EMPTY": "",
    }

    assert hide_secret_values(values) == values


def test_hide_secrets_in_text_hides_url_credentials() -> None:
    # Whatever is before the @: a user name and a password, or a token
    # alone; a password holding a raw @ is hidden whole.
    assert hide_secrets_in_text(
        "https://bob:pa55word@mcp.example.com/mcp?api_key=abc123&page=2",
    ) == f"https://{HIDDEN_VALUE}@mcp.example.com/mcp?api_key={HIDDEN_VALUE}&page=2"
    assert hide_secrets_in_text(
        "https://plaintoken@git.example.com/repo",
    ) == f"https://{HIDDEN_VALUE}@git.example.com/repo"
    assert hide_secrets_in_text(
        "postgres://admin:p@ss@db.example.com:5432/app",
    ) == f"postgres://{HIDDEN_VALUE}@db.example.com:5432/app"
    # A key in the path (Zapier, Pipedream... hand out secret URLs) or in
    # a query value under any name.
    zapier = "ZjQ5YTk3ZDItNjM4ZC00MzA0LWI2NjQtYjY5ZmJmNmI4ZTc1"
    assert hide_secrets_in_text(
        f"https://mcp.zapier.com/api/mcp/s/{zapier}/mcp",
    ) == f"https://mcp.zapier.com/api/mcp/s/{HIDDEN_VALUE}/mcp"
    assert hide_secrets_in_text(
        "https://mcp.example.com/c7c118f6-71fa-44c4-86e3-bc032facce88/sse"
        "?customer=4f9a1c2e8b7d6a5f3e2d",
    ) == f"https://mcp.example.com/{HIDDEN_VALUE}/sse?customer={HIDDEN_VALUE}"
    # A known token shape.
    assert hide_secrets_in_text(
        "https://actions.example.com/mcp/sk-ak-abcdefghijklmnopqrstuvwx/sse",
    ) == f"https://actions.example.com/mcp/{HIDDEN_VALUE}/sse"


def test_hide_secrets_in_text_shows_references_and_plain_parts() -> None:
    for text in (
        "https://mcp.example.com/mcp?token=${TOKEN}&page=2",
        "https://${USER}:${PASSWORD}@mcp.example.com/mcp",
        "https://server.smithery.ai/@owner/server/mcp?profile=work",
        "http://127.0.0.1:9001/mcp",
        # A reference is shown whatever its name looks like.
        "https://mcp.example.com/${KEY_A1B2C3D4E5F6G7H8}/mcp",
    ):
        assert hide_secrets_in_text(text) == text


def test_hide_secrets_in_text_hides_env_vars_set_on_a_command_line() -> None:
    """Like an env var's value: hidden unless only references."""
    assert hide_secrets_in_text(
        "API_KEY=abc123 LOG_LEVEL=debug node server.js",
    ) == f"API_KEY={HIDDEN_VALUE} LOG_LEVEL={HIDDEN_VALUE} node server.js"
    shown = "TOKEN=${TOKEN} node server.js"
    assert hide_secrets_in_text(shown) == shown


def test_hide_secrets_in_text_hides_what_follows_an_auth_scheme() -> None:
    assert hide_secrets_in_text(
        'curl -H "Authorization: Bearer abc123" https://mcp.example.com',
    ) == f'curl -H "Authorization: Bearer {HIDDEN_VALUE}" https://mcp.example.com'
    shown = 'curl -H "Authorization: Bearer ${TOKEN}" https://mcp.example.com'
    assert hide_secrets_in_text(shown) == shown


def test_hide_secret_args_hides_credential_arguments() -> None:
    assert hide_secret_args([
        "-y", "mcp-remote", "https://mcp.example.com/sse",
        "--header", "Authorization: Bearer abc123",
        "--header", "X-Tenant: acme",
        "--api-key", "abc123",
        "--pat", "plain-value",
        "--token=xyz789",
        "--workspace", "ab12cd34ef56gh78ij90",
        "-e", "GITHUB_TOKEN=plain-value",
        "--port", "8080",
    ]) == [
        "-y", "mcp-remote", "https://mcp.example.com/sse",
        # After --header, a header's value is hidden like in the headers.
        "--header", f"Authorization: {HIDDEN_VALUE}",
        "--header", f"X-Tenant: {HIDDEN_VALUE}",
        "--api-key", HIDDEN_VALUE,
        "--pat", HIDDEN_VALUE,
        f"--token={HIDDEN_VALUE}",
        "--workspace", HIDDEN_VALUE,
        "-e", f"GITHUB_TOKEN={HIDDEN_VALUE}",
        "--port", "8080",
    ]


def test_hide_secret_args_hides_a_whole_assigned_value() -> None:
    """An argument that sets a value hides all of it, spaces included,
    and a header set with ``--header=`` is hidden like after ``--header``."""
    assert hide_secret_args([
        "--password=two words",
        "API_KEY=a b",
        "LOG_LEVEL=debug",
        "apikey=abc",
        "mode=readonly",
        "--header=X-Api-Key: abc123",
        "--config", '{"apiKey": "plain-pass", "region": "eu"}',
    ]) == [
        f"--password={HIDDEN_VALUE}",
        f"API_KEY={HIDDEN_VALUE}",
        f"LOG_LEVEL={HIDDEN_VALUE}",
        f"apikey={HIDDEN_VALUE}",
        "mode=readonly",
        f"--header=X-Api-Key: {HIDDEN_VALUE}",
        "--config", f'{{"apiKey": "{HIDDEN_VALUE}", "region": "eu"}}',
    ]


def test_hide_secret_args_shows_references_and_package_names() -> None:
    args = [
        "--header", "Authorization:${AUTH_HEADER}", "--token", "${TOKEN}",
        "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "-e", "API_KEY=${API_KEY}",
        "mcp-neo4j-cypher@0.2.1", "--from", "mcp-server-git==0.6.2",
        "@modelcontextprotocol/server-filesystem", "localhost:8080",
    ]

    assert hide_secret_args(args) == args
