"""Tests for the ``/api/client-errors`` ingest route.

The route exists so the frontend's ``installClientErrorReporter()``
window-error / unhandledrejection handlers — and the
``reportClientError()`` helper used by silent product-flow paths
like the SignupPage submit-blocked branches — can land their
records in the same Vector → Elastic pipeline as backend logs.

The contract these tests pin down:

* The structlog event name is the client-supplied ``event`` field
  (``client.unhandled_error`` / ``client.unhandled_rejection`` /
  ``client.reported_error``) so Discover groups records on the same
  dot-separated namespace convention backend events use.
* The cookie-bearer's email (when authenticated) lands as
  ``user_email`` on the record. Anonymous reporters log
  ``user_email=None``.
* Free-text fields (``message``, ``stack``, ``url``, ``source``,
  ``release``, ``app_environment``, ``user_agent``) are clipped at
  the documented limits so a runaway producer can't spend the
  Elastic budget on a single record.
* A body larger than ``MAX_BODY_BYTES`` is rejected with 413
  *before* being parsed (defense against pydantic allocating multi-
  MB strings on a malicious POST).
* An invalid ``event`` name is rejected with 400 (pydantic Literal
  validation).
"""
from __future__ import annotations

import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import structlog
from fastapi.testclient import TestClient

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.routes.client_errors import (
    MAX_BODY_BYTES,
    MAX_FIELD_LEN,
)
from tests.unit._dev_stub_login import login_as


CONFIG_JSON: dict[str, Any] = {
    "roles": {
        "admin": {
            "is_admin": True,
            "settings": {"mcp_access": {"auto_enable_new": True}},
        },
        "user": {"is_default": True, "settings": {}},
    },
    "users": {
        "admin@example.com": {"role": "admin"},
        "alice@example.com": {"role": "user"},
    },
}


def make_test_client(tmp_path: Path) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG_JSON))
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager"
        ".UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    return TestClient(app, raise_server_exceptions=False)


def _find_event(
    logs: list[MutableMapping[str, Any]], event: str,
) -> MutableMapping[str, Any]:
    for record in logs:
        if record.get("event") == event:
            return record
    raise AssertionError(
        f"event {event!r} not in captured logs: "
        f"{[r.get('event') for r in logs]}"
    )


# ---- Happy path ------------------------------------------------------


def test_anonymous_unhandled_error_lands_as_warning(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    payload = {
        "event": "client.unhandled_error",
        "message": "ReferenceError: foo is not defined",
        "stack": "at /assets/index.js:42:7",
        "url": "https://mcphero.io/signup",
        "source": "/assets/index.js",
        "line": 42,
        "column": 7,
        "release": "abc123",
        "app_environment": "production",
    }
    with structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/api/client-errors",
            content=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (test)",
            },
        )

    assert resp.status_code == 204, resp.text
    record = _find_event(logs, "client.unhandled_error")
    assert record["log_level"] == "warning"
    assert record["message"] == payload["message"]
    assert record["stack"] == payload["stack"]
    assert record["url"] == payload["url"]
    assert record["source"] == payload["source"]
    assert record["line"] == 42
    assert record["column"] == 7
    assert record["release"] == "abc123"
    assert record["app_environment"] == "production"
    assert record["user_agent"] == "Mozilla/5.0 (test)"
    assert record["user_email"] is None


def test_authenticated_reporter_includes_user_email(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    login_as(client, "alice@example.com")

    payload = {
        "event": "client.reported_error",
        "message": "signup.blocked",
    }
    with structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/api/client-errors",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 204, resp.text
    record = _find_event(logs, "client.reported_error")
    assert record["user_email"] == "alice@example.com"
    assert record["message"] == "signup.blocked"


def test_unhandled_rejection_event_name_passes_through(tmp_path: Path) -> None:
    """The route must accept all three documented event names — the
    Literal in the pydantic schema is the contract."""
    client = make_test_client(tmp_path)
    payload = {
        "event": "client.unhandled_rejection",
        "message": "Promise rejected",
    }
    with structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/api/client-errors",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 204, resp.text
    _find_event(logs, "client.unhandled_rejection")


# ---- Bounds enforcement ---------------------------------------------


def test_oversized_body_rejected_with_413(tmp_path: Path) -> None:
    """The cap is enforced *before* pydantic parses the body so a
    malicious POST can't allocate multi-MB strings just to be rejected
    by validation later."""
    client = make_test_client(tmp_path)

    over_cap = "x" * (MAX_BODY_BYTES + 1)
    payload = {"event": "client.reported_error", "message": over_cap}
    resp = client.post(
        "/api/client-errors",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413, resp.text


def test_long_fields_are_clipped(tmp_path: Path) -> None:
    """Stack traces get a 4x budget vs other free-text fields, and
    each field is suffixed with ``...[clipped]`` so a reader knows
    the record was truncated."""
    client = make_test_client(tmp_path)

    # url over MAX_FIELD_LEN → clipped at MAX_FIELD_LEN.
    long_url = "https://example.com/" + ("y" * (MAX_FIELD_LEN + 50))
    # stack over (MAX_FIELD_LEN * 4) → clipped at the larger 4x budget.
    # Sized to fit alongside ``long_url`` inside MAX_BODY_BYTES.
    long_stack = "z" * (MAX_FIELD_LEN * 4 + 200)
    payload = {
        "event": "client.reported_error",
        "message": "boom",
        "url": long_url,
        "stack": long_stack,
    }
    with structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/api/client-errors",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 204, resp.text
    record = _find_event(logs, "client.reported_error")
    assert record["url"].endswith("...[clipped]")
    assert len(record["url"]) <= MAX_FIELD_LEN + len("...[clipped]")
    assert record["stack"].endswith("...[clipped]")
    assert len(record["stack"]) <= MAX_FIELD_LEN * 4 + len("...[clipped]")


def test_unknown_event_name_rejected_with_400(tmp_path: Path) -> None:
    """The pydantic Literal restricts ``event`` to the three documented
    names — the structlog event name is the namespace bucket Discover
    groups by, so accepting arbitrary strings would let a misbehaving
    frontend explode the cardinality of that field."""
    client = make_test_client(tmp_path)
    payload = {
        "event": "client.something.made.up",
        "message": "...",
    }
    resp = client.post(
        "/api/client-errors",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400, resp.text


def test_invalid_json_rejected_with_400(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    resp = client.post(
        "/api/client-errors",
        content=b"this is not JSON",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400, resp.text
