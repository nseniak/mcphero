"""Tests for the standalone-mode file-backed Sandbox-files store."""
from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_sandbox_file_repository import (
    FileSandboxFileRepository,
)


def make_repo(tmp_path: Path) -> FileSandboxFileRepository:
    return FileSandboxFileRepository(tmp_path)


@pytest.mark.asyncio
async def test_set_then_get_round_trips_contents(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set(
        "default", "gcp", "GCP_CRED",
        '{"type":"service_account"}',
        "${HOME}/.config/gcloud/credentials.json",
    )
    got = await repo.get("default", "gcp", "GCP_CRED")
    assert got is not None
    assert got.contents == '{"type":"service_account"}'
    assert got.target_path == "${HOME}/.config/gcloud/credentials.json"


@pytest.mark.asyncio
async def test_list_summaries_excludes_contents(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set(
        "default", "gcp", "GCP_CRED", "{}", "${HOME}/x",
    )
    summaries = await repo.list_summaries("default", "gcp")
    assert len(summaries) == 1
    assert summaries[0].name == "GCP_CRED"
    # Summary type literally has no contents field.
    assert "contents" not in summaries[0].model_dump()


@pytest.mark.asyncio
async def test_set_replaces_keeps_created_at(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = await repo.set(
        "default", "gcp", "GCP_CRED", "v1", "${HOME}/x",
    )
    second = await repo.set(
        "default", "gcp", "GCP_CRED", "v2", "${HOME}/x",
    )
    assert first.created_at == second.created_at
    assert second.updated_at >= first.updated_at
    got = await repo.get("default", "gcp", "GCP_CRED")
    assert got is not None
    assert got.contents == "v2"


@pytest.mark.asyncio
async def test_delete_removes_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "gcp", "GCP_CRED", "v", "${HOME}/x")
    await repo.delete("default", "gcp", "GCP_CRED")
    assert await repo.get("default", "gcp", "GCP_CRED") is None
    assert await repo.list_summaries("default", "gcp") == []


@pytest.mark.asyncio
async def test_delete_all_cascades_on_upstream_removal(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "gcp", "A", "v", "${HOME}/a")
    await repo.set("default", "gcp", "B", "v", "${HOME}/b")
    await repo.delete_all("default", "gcp")
    assert await repo.list_summaries("default", "gcp") == []


@pytest.mark.asyncio
async def test_per_upstream_isolation(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "gcp", "X", "v1", "${HOME}/gcp")
    await repo.set("default", "k8s", "X", "v2", "${HOME}/k8s")
    a = await repo.get("default", "gcp", "X")
    b = await repo.get("default", "k8s", "X")
    assert a is not None and b is not None
    assert a.contents == "v1"
    assert b.contents == "v2"


@pytest.mark.asyncio
async def test_sha256_and_size_computed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    summary = await repo.set(
        "default", "gcp", "GCP_CRED", "hello", "${HOME}/h",
    )
    assert summary.size_bytes == 5
    # SHA-256 of "hello"
    assert summary.sha256 == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


@pytest.mark.asyncio
async def test_list_full_carries_contents(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "gcp", "X", "secret-body", "${HOME}/x")
    rows = await repo.list_full("default", "gcp")
    assert len(rows) == 1
    assert rows[0].contents == "secret-body"
