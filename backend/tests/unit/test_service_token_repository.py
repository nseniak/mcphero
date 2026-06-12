"""Service-token registry tests — file and Mongo implementations.

Mongo tests are skipped automatically when no test Mongo is reachable
(see ``mongo_fixture.MONGO_URI``).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.adapters.repositories.mongo_client import COLL_SERVICE_TOKENS
from mcpolis.adapters.repositories.mongo_service_token_repository import (
    MongoServiceTokenRepository,
)
from mcpolis.domain.model.service_token import hash_service_token
from mcpolis.domain.ports.service_token_repository import (
    DuplicateServiceTokenLabelError,
)

from tests.unit.factories import make_service_token_record
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


def make_file_repo(tmp_path: Path) -> FileServiceTokenRepository:
    return FileServiceTokenRepository(tmp_path)


@pytest.mark.asyncio
async def test_file_repo_create_list_get_delete_roundtrip(
    tmp_path: Path,
) -> None:
    repo = make_file_repo(tmp_path)
    record = make_service_token_record(label="ci-bot", role_name="user")
    await repo.create(record)

    listed = await repo.list_for_org(record.org_id)
    assert [r.label for r in listed] == ["ci-bot"]
    assert listed[0].role_name == "user"
    assert listed[0].token_hash == record.token_hash

    by_hash = await repo.get_by_hash(record.token_hash)
    assert by_hash is not None
    assert by_hash.label == "ci-bot"
    assert by_hash.org_id == record.org_id

    by_label = await repo.get_by_label(record.org_id, "ci-bot")
    assert by_label is not None
    assert by_label.token_hash == record.token_hash

    assert await repo.delete_by_label(record.org_id, "ci-bot") is True
    assert await repo.list_for_org(record.org_id) == []
    assert await repo.get_by_hash(record.token_hash) is None
    assert await repo.delete_by_label(record.org_id, "ci-bot") is False


@pytest.mark.asyncio
async def test_file_repo_duplicate_label_raises(tmp_path: Path) -> None:
    repo = make_file_repo(tmp_path)
    await repo.create(make_service_token_record(label="bot"))
    with pytest.raises(DuplicateServiceTokenLabelError):
        await repo.create(
            make_service_token_record(label="bot", raw_token="svct_other"),
        )


@pytest.mark.asyncio
async def test_file_repo_same_label_different_orgs_ok(
    tmp_path: Path,
) -> None:
    repo = make_file_repo(tmp_path)
    await repo.create(
        make_service_token_record(
            label="bot", org_id="org-a", raw_token="svct_a",
        ),
    )
    await repo.create(
        make_service_token_record(
            label="bot", org_id="org-b", raw_token="svct_b",
        ),
    )
    assert len(await repo.list_for_org("org-a")) == 1
    assert len(await repo.list_for_org("org-b")) == 1
    # Each org's record carries its own hash.
    a = await repo.get_by_label("org-a", "bot")
    b = await repo.get_by_label("org-b", "bot")
    assert a is not None and b is not None
    assert a.token_hash != b.token_hash


@pytest.mark.asyncio
async def test_file_repo_touch_last_used_persists(tmp_path: Path) -> None:
    repo = make_file_repo(tmp_path)
    record = make_service_token_record()
    await repo.create(record)
    when = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await repo.touch_last_used(record.token_hash, when)

    # Re-read through a fresh instance to prove it hit disk.
    fresh = make_file_repo(tmp_path)
    reloaded = await fresh.get_by_hash(record.token_hash)
    assert reloaded is not None
    assert reloaded.last_used_at == when
    # Unknown hash is a no-op, not an error.
    await fresh.touch_last_used(hash_service_token("svct_ghost"), when)


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_mongo_repo_roundtrip() -> None:
    async with temp_mongo_database() as db:
        repo = MongoServiceTokenRepository(db[COLL_SERVICE_TOKENS])
        record = make_service_token_record(label="ci-bot")
        await repo.create(record)

        by_hash = await repo.get_by_hash(record.token_hash)
        assert by_hash is not None
        assert by_hash.label == "ci-bot"
        assert by_hash.org_id == record.org_id

        when = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        await repo.touch_last_used(record.token_hash, when)
        touched = await repo.get_by_label(record.org_id, "ci-bot")
        assert touched is not None
        assert touched.last_used_at == when

        assert await repo.delete_by_label(record.org_id, "ci-bot") is True
        assert await repo.get_by_hash(record.token_hash) is None


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_mongo_repo_duplicate_label_raises() -> None:
    async with temp_mongo_database() as db:
        repo = MongoServiceTokenRepository(db[COLL_SERVICE_TOKENS])
        await repo.create(make_service_token_record(label="bot"))
        with pytest.raises(DuplicateServiceTokenLabelError):
            await repo.create(
                make_service_token_record(
                    label="bot", raw_token="svct_other",
                ),
            )


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_mongo_repo_list_is_org_scoped_no_cross_org_leak() -> None:
    async with temp_mongo_database() as db:
        repo = MongoServiceTokenRepository(db[COLL_SERVICE_TOKENS])
        await repo.create(
            make_service_token_record(
                label="bot-a", org_id="org-a", raw_token="svct_a",
            ),
        )
        await repo.create(
            make_service_token_record(
                label="bot-b", org_id="org-b", raw_token="svct_b",
            ),
        )
        a_list = await repo.list_for_org("org-a")
        assert [r.label for r in a_list] == ["bot-a"]
        # delete_by_label must not cross org boundaries either.
        assert await repo.delete_by_label("org-a", "bot-b") is False
        assert await repo.get_by_label("org-b", "bot-b") is not None


@pytest.mark.asyncio
async def test_file_repo_delete_for_org_cascades(tmp_path: Path) -> None:
    repo = make_file_repo(tmp_path)
    await repo.create(
        make_service_token_record(label="a", org_id="org-a", raw_token="svct_a"),
    )
    await repo.create(
        make_service_token_record(label="b", org_id="org-a", raw_token="svct_b"),
    )
    await repo.create(
        make_service_token_record(label="c", org_id="org-b", raw_token="svct_c"),
    )
    assert await repo.delete_for_org("org-a") == 2
    assert await repo.list_for_org("org-a") == []
    # Other orgs untouched; idempotent on a now-empty org.
    assert len(await repo.list_for_org("org-b")) == 1
    assert await repo.delete_for_org("org-a") == 0


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_mongo_repo_delete_for_org_cascades() -> None:
    async with temp_mongo_database() as db:
        repo = MongoServiceTokenRepository(db[COLL_SERVICE_TOKENS])
        await repo.create(
            make_service_token_record(
                label="a", org_id="org-a", raw_token="svct_a",
            ),
        )
        await repo.create(
            make_service_token_record(
                label="c", org_id="org-b", raw_token="svct_c",
            ),
        )
        assert await repo.delete_for_org("org-a") == 1
        assert await repo.get_by_label("org-b", "c") is not None
