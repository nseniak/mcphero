"""Contract test: the structlog output shape the Vector sidecar relies on.

Vector's ``parse`` transform in
[compose/vector/vector.toml](../../../compose/vector/vector.toml) reads three
top-level fields from each JSON line on the backend's stdout:

  - ``event``      — the dotted event name (first positional arg)
  - ``level``      — added by ``structlog.stdlib.add_log_level``
  - ``timestamp``  — added by ``TimeStamper(fmt="iso", utc=True)`` and
                    renamed to ``@timestamp`` by Vector's VRL

If a future change to ``configure_structlog`` drops or renames any of these
keys, the prod log pipeline silently breaks (records ship to Elastic with
no event, no severity, or no time field, and Discover becomes useless).
This test is the trip-wire.

The test reaches into ``configure_structlog`` directly, captures stdout,
and round-trips one JSON line. We avoid ``structlog.testing.capture_logs``
deliberately: that helper short-circuits the processor chain and would
miss exactly the fields we care about (``level``, ``timestamp``).
"""
from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Callable
from typing import Any

import structlog

from mcpolis.adapters.observability.structlog_setup import configure_structlog


def _emit_and_capture(emit: Callable[[], None]) -> dict[str, Any]:
    """Configure structlog with ``json_logs=True``, run ``emit``, restore.

    Returns the last JSON line written to stdout, parsed as a dict.

    Restoration is non-trivial because ``configure_structlog`` mutates
    process-global state (root logger handlers + structlog default
    config). We snapshot before, swap stdout, run, then put everything
    back so this test doesn't bleed into other tests in the suite.
    """
    saved_handlers = list(logging.root.handlers)
    saved_level = logging.root.level
    saved_stdout = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        # configure_structlog reads sys.stdout *now* and binds the
        # handler to it; the swap above is what lets us capture output.
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
    last_line = raw.splitlines()[-1]
    parsed: dict[str, Any] = json.loads(last_line)
    return parsed


def test_json_log_has_event_field() -> None:
    parsed = _emit_and_capture(
        lambda: structlog.get_logger("mcpolis.test").info("contract.test.event"),
    )
    assert parsed["event"] == "contract.test.event"


def test_json_log_has_level_field() -> None:
    parsed = _emit_and_capture(
        lambda: structlog.get_logger("mcpolis.test").warning("ev"),
    )
    assert parsed["level"] == "warning"


def test_json_log_has_iso_timestamp_field() -> None:
    parsed = _emit_and_capture(
        lambda: structlog.get_logger("mcpolis.test").info("ev"),
    )
    assert "timestamp" in parsed, "structlog must emit a `timestamp` field"
    # TimeStamper(fmt="iso", utc=True) → ISO-8601 with trailing Z.
    assert isinstance(parsed["timestamp"], str)
    assert parsed["timestamp"].endswith("Z")


def test_json_log_kwargs_become_top_level_fields() -> None:
    parsed = _emit_and_capture(
        lambda: structlog.get_logger("mcpolis.test").info(
            "ev", org_id="org-1", user_email="alice@example.com",
        ),
    )
    assert parsed["org_id"] == "org-1"
    assert parsed["user_email"] == "alice@example.com"


def test_bound_contextvars_appear_on_stdlib_logger_records() -> None:
    """The whole point of routing stdlib loggers through
    ``ProcessorFormatter``'s ``foreign_pre_chain`` is that
    ``merge_contextvars`` runs on those records too — so any
    ``bound_contextvars(...)`` block enriches third-party output
    (``mcp.client.auth.oauth2``, ``httpx``, ``uvicorn.access``) with the
    same org/upstream/user fields our own log lines carry.

    Without this contract, the MCPOLIS-BACKEND-C "OAuth flow error"
    records (and any future SDK ERROR) reach Elastic as a bare stacktrace
    with no business context — exactly the gap the periodic-loop wrapper
    in ``_periodic_token_refresh_all`` depends on this bridge to close.
    Regression-guards both ``merge_contextvars`` staying in the shared
    chain AND ``foreign_pre_chain`` continuing to apply it to stdlib
    records."""
    from structlog.contextvars import bound_contextvars

    def _emit_inside_bound_context() -> None:
        # Use a stdlib logger (not a structlog one) to prove the bridge.
        # ``mcp.client.auth.oauth2`` is the exact logger that emits the
        # context-less "OAuth flow error" line in prod.
        stdlib = logging.getLogger("mcp.client.auth.oauth2")
        with bound_contextvars(
            org_id="org-x",
            upstream_id="meerbot",
            user="alice@example.com",
        ):
            stdlib.error("OAuth flow error")

    parsed = _emit_and_capture(_emit_inside_bound_context)

    assert parsed["event"] == "OAuth flow error"
    assert parsed["org_id"] == "org-x"
    assert parsed["upstream_id"] == "meerbot"
    assert parsed["user"] == "alice@example.com"
    # The logger name is preserved so operators can still filter on it
    # alongside the now-bound business context.
    assert parsed["logger"] == "mcp.client.auth.oauth2"
