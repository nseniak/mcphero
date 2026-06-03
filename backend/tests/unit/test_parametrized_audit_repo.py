"""Parameterized ``AuditRepository`` tests."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.mongo_audit_repository import MongoAuditRepository
from mcpolis.adapters.repositories.mongo_client import (
    COLL_AUDIT,
    OrgScopedCollection,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit.factories import make_audit_entry
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


BACKENDS: list[str] = ["file"] + (["mongo"] if mongo_available() else [])


@asynccontextmanager
async def _make_repo(backend: str, tmp_path: Path) -> AsyncIterator[AuditRepository]:
    if backend == "file":
        yield FileAuditRepository(tmp_path / "audit.jsonl")
        return
    async with temp_mongo_database() as db:
        coll = OrgScopedCollection(db[COLL_AUDIT], COLL_AUDIT)
        yield MongoAuditRepository(coll)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_log_and_search(backend: str, tmp_path: Path) -> None:
    async with _make_repo(backend, tmp_path) as repo:
        await repo.log(
            DEFAULT_ORG_ID,
            make_audit_entry(user_id="alice", tool="github__create_issue"),
        )
        await repo.log(
            DEFAULT_ORG_ID,
            make_audit_entry(user_id="bob", tool="slack__post"),
        )
        results = await repo.search(DEFAULT_ORG_ID, limit=10)
        assert len(results) == 2
        user_ids = {r["user_id"] for r in results}
        assert user_ids == {"alice", "bob"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_filter_by_user(backend: str, tmp_path: Path) -> None:
    async with _make_repo(backend, tmp_path) as repo:
        await repo.log(
            DEFAULT_ORG_ID, make_audit_entry(user_id="alice"),
        )
        await repo.log(
            DEFAULT_ORG_ID, make_audit_entry(user_id="bob"),
        )
        results: list[dict[str, Any]] = await repo.search(
            DEFAULT_ORG_ID, user_id="alice", limit=10,
        )
        assert len(results) == 1
        assert results[0]["user_id"] == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_get_filter_values(backend: str, tmp_path: Path) -> None:
    async with _make_repo(backend, tmp_path) as repo:
        await repo.log(
            DEFAULT_ORG_ID,
            make_audit_entry(user_id="alice", upstream_id="mcp-a"),
        )
        await repo.log(
            DEFAULT_ORG_ID,
            make_audit_entry(user_id="bob", upstream_id="mcp-b"),
        )
        filters = await repo.get_filter_values(DEFAULT_ORG_ID)
        assert "alice" in filters["user_ids"]
        assert "bob" in filters["user_ids"]
        assert "mcp-a" in filters["upstream_ids"]
        assert "mcp-b" in filters["upstream_ids"]
