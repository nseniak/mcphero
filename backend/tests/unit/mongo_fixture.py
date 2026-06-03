"""Shared helpers for the Mongo-parameterized repo tests.

``MCPOLIS_TEST_MONGO_URI`` selects the Mongo endpoint — default is
``mongodb://localhost:27017``. If the variable is empty or the probe
below cannot connect, the Mongo parameters are skipped and tests run
file-only. Each test gets a throwaway ``mcpolis_test_<uuid>`` database
that is dropped at the end of the test function, so the real
``mcpolis`` database is never touched even when the same instance is
used for dev and tests.
"""
from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("motor")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from mcpolis.adapters.repositories.mongo_client import (  # noqa: E402
    MotorDatabase,
    create_indexes,
)


def _probe(uri: str) -> bool:
    """Parse the host:port out of a Mongo URI and do a quick TCP probe."""
    host = "localhost"
    port = 27017
    scheme, _, rest = uri.partition("://")
    _ = scheme
    # Strip credentials if any.
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    # Strip path/query.
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if ":" in rest:
        hp = rest.split(",", 1)[0]
        host, _, port_s = hp.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            return False
    elif rest:
        host = rest
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _resolve_uri() -> str | None:
    configured = os.environ.get("MCPOLIS_TEST_MONGO_URI", "").strip()
    if not configured:
        return None
    return configured if _probe(configured) else None


MONGO_URI: str | None = _resolve_uri()


def mongo_available() -> bool:
    return MONGO_URI is not None


def require_mongo() -> str:
    if MONGO_URI is None:
        pytest.skip("Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)")
    return MONGO_URI


@asynccontextmanager
async def temp_mongo_database() -> AsyncIterator[MotorDatabase]:
    """Yield a throwaway database that is dropped at scope exit.

    Always creates a unique database name, so parallel runs and the
    user's real ``mcpolis`` database are both safe.
    """
    uri = require_mongo()
    client: AsyncIOMotorClient[dict[str, object]] = AsyncIOMotorClient(uri)
    db_name = f"mcpolis_test_{uuid.uuid4().hex[:12]}"
    try:
        db: MotorDatabase = client[db_name]
        await create_indexes(db)
        yield db
    finally:
        try:
            await client.drop_database(db_name)
        finally:
            client.close()
