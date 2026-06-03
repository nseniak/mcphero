"""HMAC-signed stateless tokens.

Used for OAuth state tokens and dashboard session cookies.
Format: base64url(payload) + "." + base64url(hmac-sha256).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def sign_token(payload: dict[str, Any], key: bytes) -> str:
    """Create a signed token: base64url(payload).base64url(hmac)."""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode()
    sig = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode()
    return f"{payload_b64}.{sig_b64}"


def verify_token(
    token: str, key: bytes, *, max_age: int | None = None
) -> dict[str, Any] | None:
    """Verify and decode a signed token.

    Returns the payload dict if valid, None otherwise.
    If *max_age* is given (seconds), the token is rejected when
    ``time.time() - payload["iat"]`` exceeds that value.
    """
    parts = token.split(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        expected_sig = hmac.new(key, payload_bytes, hashlib.sha256).digest()
        actual_sig = base64.urlsafe_b64decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        return None
    # Check expiry via "exp" (absolute) or "iat" + max_age
    exp = payload.get("exp")
    if exp is not None and exp < time.time():
        return None
    if max_age is not None:
        iat = payload.get("iat")
        if iat is None or time.time() - iat > max_age:
            return None
    return payload  # type: ignore[no-any-return]
