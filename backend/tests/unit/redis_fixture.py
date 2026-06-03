"""Shared helpers for the Phase 2d Redis-backed tests.

``MCPOLIS_TEST_REDIS_URL`` selects the Redis endpoint — default is
``redis://localhost:6379/15`` (db 15 by convention to stay off dev/prod
data). If the variable is empty or the probe below cannot connect,
the Redis-backed tests are skipped. Each test is responsible for
flushing its own keys before starting (keys are namespaced by
``test:{uuid}`` to avoid collisions across parallel runs).
"""
from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest

_DEFAULT_URL = "redis://localhost:6379/15"


def _probe(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _resolve_url() -> str | None:
    configured = os.environ.get("MCPOLIS_TEST_REDIS_URL", _DEFAULT_URL).strip()
    if not configured:
        return None
    return configured if _probe(configured) else None


REDIS_URL: str | None = _resolve_url()


def redis_available() -> bool:
    return REDIS_URL is not None


def require_redis() -> str:
    if REDIS_URL is None:
        pytest.skip("Redis not reachable (set MCPOLIS_TEST_REDIS_URL)")
    return REDIS_URL
