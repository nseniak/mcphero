"""Unit tests for the structlog secret-key redaction processor.

Layer 1 of the F-02 fix: every structlog record passes through a
recursive walker that masks values at any key matching
``_SECRET_RE`` with ``[REDACTED]`` before the JSON renderer turns
the event dict into a stdout line. This is what stops a
``log.info("ev", config=cfg)`` from leaking ``cfg["api_key"]`` into
Elastic.

Contract:

- The match is on dict *keys*, case-insensitive. Values are never
  inspected (we don't try to detect a JWT-shaped string in a key
  named ``foo``).
- Recursion descends into nested dicts and into lists/tuples of
  dicts. List elements that are themselves not dicts are passed
  through unchanged — the Caddy/uvicorn-shaped ``headers: [{name,
  value}]`` array is *not* redacted here; that case goes through
  Layer 2 (Vector's ``del(.request.headers.Cookie)``).
- Non-container scalars (str, int, None) pass through untouched.

Tests are toplevel functions, no fixtures (per CLAUDE.md).
"""
from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Callable
from typing import Any

import structlog

from mcpolis.adapters.observability.redact_processor import (
    _SECRET_RE,
    redact_secret_keys,
)
from mcpolis.adapters.observability.structlog_setup import configure_structlog


# ---- Pure walker tests ----


def test_redacts_top_level_password() -> None:
    out = redact_secret_keys(None, "info", {"password": "hunter2", "user": "alice"})
    assert out == {"password": "[REDACTED]", "user": "alice"}


def test_redacts_nested_api_key() -> None:
    out = redact_secret_keys(
        None,
        "info",
        {"config": {"api_key": "sk-abc", "model": "claude"}},
    )
    assert out == {"config": {"api_key": "[REDACTED]", "model": "claude"}}


def test_does_not_redact_inside_list_of_dict_with_value_key() -> None:
    """Caddy/uvicorn-shaped ``[{name, value}]`` headers are not redacted
    here — the key being matched is ``name``, not ``Authorization``.
    Layer 2 (Vector) is responsible for that case."""
    inp = {"headers": [{"name": "Authorization", "value": "Bearer xyz"}]}
    out = redact_secret_keys(None, "info", inp)
    assert out == inp


def test_redacts_top_level_authorization_case_insensitive() -> None:
    out = redact_secret_keys(None, "info", {"Authorization": "Bearer xyz"})
    assert out == {"Authorization": "[REDACTED]"}


def test_redacts_client_secret_but_not_client_id() -> None:
    out = redact_secret_keys(
        None, "info", {"client_secret": "x", "client_id": "y"},
    )
    assert out == {"client_secret": "[REDACTED]", "client_id": "y"}


def test_redacts_deeply_nested_token() -> None:
    out = redact_secret_keys(
        None, "info", {"nested": {"deep": {"token": "x"}}},
    )
    assert out == {"nested": {"deep": {"token": "[REDACTED]"}}}


def test_redacts_inside_list_of_dicts() -> None:
    out = redact_secret_keys(
        None,
        "info",
        {"items": [{"password": "a", "name": "alice"}, {"password": "b"}]},
    )
    assert out == {
        "items": [
            {"password": "[REDACTED]", "name": "alice"},
            {"password": "[REDACTED]"},
        ],
    }


def test_passthrough_string_input() -> None:
    out = redact_secret_keys(None, "info", "plain string")  # type: ignore[arg-type]
    assert out == "plain string"


def test_passthrough_none_input() -> None:
    out = redact_secret_keys(None, "info", None)  # type: ignore[arg-type]
    assert out is None


def test_passthrough_non_secret_keys_unchanged() -> None:
    inp = {"event": "ev", "level": "info", "org_id": "o-1", "user": "u"}
    out = redact_secret_keys(None, "info", inp)
    assert out == inp


def test_secret_re_matches_expected_keys() -> None:
    for key in [
        "password",
        "Password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api-key",
        "Authorization",
        "cookie",
        "Cookie",
        "client_secret",
        "client-secret",
        "bearer",
        "signing_key",
        "access_key",
    ]:
        assert _SECRET_RE.search(key), f"expected {key!r} to match _SECRET_RE"


def test_secret_re_does_not_match_innocuous_keys() -> None:
    for key in ["event", "level", "org_id", "user_email", "method", "path"]:
        assert not _SECRET_RE.search(key), f"unexpected match on {key!r}"


# ---- structlog end-to-end integration ----


def _emit_and_capture(emit: Callable[[], None]) -> dict[str, Any]:
    """Run ``configure_structlog`` and capture one JSON line from stdout."""
    saved_handlers = list(logging.root.handlers)
    saved_level = logging.root.level
    saved_stdout = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        configure_structlog(json_logs=True, log_level="INFO")
        emit()
    finally:
        sys.stdout = saved_stdout
        for handler in list(logging.root.handlers):
            logging.root.removeHandler(handler)
        for handler in saved_handlers:
            logging.root.addHandler(handler)
        logging.root.setLevel(saved_level)
        structlog.reset_defaults()

    raw = buf.getvalue().strip()
    assert raw, "expected at least one JSON line on stdout"
    return json.loads(raw.splitlines()[-1])


def test_structlog_chain_redacts_top_level_password_kwarg() -> None:
    parsed = _emit_and_capture(
        lambda: structlog.get_logger("mcpolis.test").info(
            "auth.attempt", password="hunter2", user="alice",
        ),
    )
    assert parsed["event"] == "auth.attempt"
    assert parsed["password"] == "[REDACTED]"
    assert parsed["user"] == "alice"


def test_structlog_chain_redacts_nested_config_dict() -> None:
    parsed = _emit_and_capture(
        lambda: structlog.get_logger("mcpolis.test").info(
            "upstream.configured",
            config={"api_key": "sk-abc", "model": "claude"},
        ),
    )
    assert parsed["config"]["api_key"] == "[REDACTED]"
    assert parsed["config"]["model"] == "claude"


def test_structlog_chain_preserves_non_secret_kwargs() -> None:
    parsed = _emit_and_capture(
        lambda: structlog.get_logger("mcpolis.test").info(
            "ev", org_id="org-1", user_email="alice@example.com",
        ),
    )
    assert parsed["org_id"] == "org-1"
    assert parsed["user_email"] == "alice@example.com"
    assert parsed["event"] == "ev"
