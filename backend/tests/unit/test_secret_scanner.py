"""Tests for the defensive secret scanner.

The scanner runs on save and emits ``secret_in_json_detected`` events.
These tests confirm each pattern fires and that benign values don't
trigger false positives. None of the tests assert on log calls — the
scanner is a pure function, the logging is the caller's job.
"""
from __future__ import annotations

from mcpolis.domain.services.secret_scanner import scan_for_secrets


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
