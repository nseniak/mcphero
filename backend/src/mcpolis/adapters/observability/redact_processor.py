"""Structlog processor: mask values at secret-shaped keys.

Layer 1 of the F-02 fix. Walks the event dict before the JSON
renderer and replaces any value at a key matching ``_SECRET_RE``
with ``[REDACTED]``. This is what stops a careless
``log.info("ev", config=cfg)`` or ``log.bind(headers=...)`` from
landing OAuth tokens, API keys, or session cookies in
``logs-mcpolis-prod`` cleartext.

Contract:

- Match is on dict *keys*, case-insensitive.
- Recursion descends into nested dicts and into lists/tuples that
  contain dicts. Plain scalars (str, int, None) pass through. List
  elements that aren't themselves dicts are left alone — the
  Caddy/uvicorn-shaped ``[{name, value}]`` headers array is *not*
  redacted at this layer; that case is handled by the Vector
  transform's explicit ``del(.request.headers.Cookie)`` etc.
- Acceptable false-positives: ``mfa_token_count`` becomes
  ``[REDACTED]``. Over-redacting a counter is fine; the alternative
  (an allowlist) is fragile.
"""
from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"

_SECRET_RE = re.compile(
    r"(?i)("
    r"password|secret|token|api[_-]?key|authorization|"
    r"cookie|client[_-]?secret|bearer|signing[_-]?key|access[_-]?key"
    r")",
)


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for k, v in value.items():  # type: ignore[reportUnknownVariableType]
            if isinstance(k, str) and _SECRET_RE.search(k):
                result[k] = _REDACTED
            else:
                result[k] = _walk(v)
        return result
    if isinstance(value, list):
        return [_walk(item) for item in value]  # type: ignore[reportUnknownVariableType]
    if isinstance(value, tuple):
        return tuple(_walk(item) for item in value)  # type: ignore[reportUnknownVariableType]
    return value


def redact_secret_keys(_logger: Any, _method: str, event_dict: Any) -> Any:
    """Structlog processor entry point.

    Returns a new event dict with secret-shaped keys' values masked.
    Pass-through for non-dict inputs so the function is also useful
    as a plain helper in tests.
    """
    return _walk(event_dict)
