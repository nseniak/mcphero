"""Parameterized ``ConfigRepository`` tests."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONFIG,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_config_repository import (
    MongoConfigRepository,
)
from mcpolis.domain.model.settings import UserDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.config_repository import ConfigRepository
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


BACKENDS: list[str] = ["file"] + (["mongo"] if mongo_available() else [])


@asynccontextmanager
async def _make_store(backend: str, tmp_path: Path) -> AsyncIterator[ConfigRepository]:
    if backend == "file":
        store = FileConfigStore(tmp_path / "config.json")
        await store.ensure_defaults(DEFAULT_ORG_ID)
        yield store
        return
    async with temp_mongo_database() as db:
        coll = OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG)
        store2 = MongoConfigRepository(coll)
        await store2.ensure_defaults(DEFAULT_ORG_ID)
        yield store2


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_defaults_seeded(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        config = await store.load(DEFAULT_ORG_ID)
        assert "admin" in config.roles
        assert "user" in config.roles


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_set_and_remove_user(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        config = await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin")
        )
        assert "alice@test.com" in config.users
        assert config.users["alice@test.com"].role == "admin"
        config = await store.remove_user(DEFAULT_ORG_ID, "alice@test.com")
        assert "alice@test.com" not in config.users


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_create_and_delete_role(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        config = await store.create_role(DEFAULT_ORG_ID, "operator")
        assert "operator" in config.roles
        config = await store.delete_role(DEFAULT_ORG_ID, "operator")
        assert "operator" not in config.roles


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_cross_org_isolation(backend: str, tmp_path: Path) -> None:
    """org_a's writes must not leak into org_b's reads."""
    async with _make_store(backend, tmp_path) as store:
        # File store is single-org by design, so only test cross-org
        # isolation on the Mongo backend.
        if backend == "file":
            pytest.skip("file store is single-default-org by design")
        # Same repo, different orgs — only Mongo actually partitions.
        other_org = "other-org"
        await store.ensure_defaults(other_org)
        await store.set_user(
            DEFAULT_ORG_ID, "alice@test.com", UserDefinition(role="admin")
        )
        other = await store.load(other_org)
        assert "alice@test.com" not in other.users
