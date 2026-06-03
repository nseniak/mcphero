"""Tests for the shared HMAC token utility."""
from __future__ import annotations

import hashlib
import time

from mcpolis.adapters.auth.hmac_token import sign_token, verify_token


def make_key() -> bytes:
    return hashlib.sha256(b"test-secret").digest()


def test_sign_and_verify_roundtrip() -> None:
    key = make_key()
    payload = {"user": "alice", "iat": time.time()}
    token = sign_token(payload, key)
    result = verify_token(token, key)
    assert result is not None
    assert result["user"] == "alice"


def test_verify_rejects_tampered_payload() -> None:
    key = make_key()
    token = sign_token({"user": "alice", "iat": time.time()}, key)
    # Tamper with the payload portion
    parts = token.split(".", 1)
    tampered = "x" + parts[0][1:] + "." + parts[1]
    assert verify_token(tampered, key) is None


def test_verify_rejects_wrong_key() -> None:
    key = make_key()
    token = sign_token({"user": "alice", "iat": time.time()}, key)
    wrong_key = hashlib.sha256(b"wrong").digest()
    assert verify_token(token, wrong_key) is None


def test_verify_rejects_expired_token() -> None:
    key = make_key()
    token = sign_token({"user": "alice", "exp": time.time() - 10}, key)
    assert verify_token(token, key) is None


def test_verify_max_age() -> None:
    key = make_key()
    # Token issued 100 seconds ago
    token = sign_token({"user": "alice", "iat": time.time() - 100}, key)
    # max_age=200 → still valid
    assert verify_token(token, key, max_age=200) is not None
    # max_age=50 → expired
    assert verify_token(token, key, max_age=50) is None


def test_verify_rejects_garbage() -> None:
    key = make_key()
    assert verify_token("not.a.token", key) is None
    assert verify_token("garbage", key) is None
    assert verify_token("", key) is None
