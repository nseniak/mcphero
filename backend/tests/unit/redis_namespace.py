"""Unique per-test Redis key prefix so parallel runs never collide."""
from __future__ import annotations

import uuid


def make_redis_namespace() -> str:
    """Return a fresh ``test:{uuid4}:`` Redis key prefix unique per call."""
    return f"test:{uuid.uuid4()}:"


__all__ = ["make_redis_namespace"]
