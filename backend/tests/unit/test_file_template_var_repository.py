"""Tests for the standalone-mode file-backed secret store."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)


def make_repo(tmp_path: Path) -> FileTemplateVarRepository:
    return FileTemplateVarRepository(tmp_path)


@pytest.mark.asyncio
async def test_set_then_get_round_trips_value(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "github", "TOKEN", "secret-value-1234")
    assert await repo.get_value("default", "github", "TOKEN") == "secret-value-1234"


@pytest.mark.asyncio
async def test_list_summaries_returns_value_for_password_rows(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "github", "TOKEN", "x" * 32)
    summaries = await repo.list_summaries("default", "github")
    assert len(summaries) == 1
    assert summaries[0].name == "TOKEN"
    assert summaries[0].last_four == "xxxx"
    # The list path now carries the plaintext for password rows too —
    # the SPA obfuscates by default and exposes an eye toggle.
    assert summaries[0].is_secret is True
    assert summaries[0].value == "x" * 32


@pytest.mark.asyncio
async def test_list_summaries_sorted_by_name(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "github", "B_TOKEN", "v" * 32)
    await repo.set("default", "github", "A_TOKEN", "v" * 32)
    summaries = await repo.list_summaries("default", "github")
    assert [s.name for s in summaries] == ["A_TOKEN", "B_TOKEN"]


@pytest.mark.asyncio
async def test_set_replaces_value_but_keeps_created_at(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = await repo.set("default", "github", "TOKEN", "first-value-1234")
    second = await repo.set("default", "github", "TOKEN", "second-value-1234")
    assert first.created_at == second.created_at
    assert second.updated_at >= first.updated_at
    assert await repo.get_value("default", "github", "TOKEN") == "second-value-1234"


@pytest.mark.asyncio
async def test_short_values_have_no_last_four(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    summary = await repo.set("default", "github", "TOKEN", "short")
    assert summary.last_four is None


@pytest.mark.asyncio
async def test_delete_removes_secret(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "github", "TOKEN", "value-is-a-secret")
    await repo.delete("default", "github", "TOKEN")
    assert await repo.get_value("default", "github", "TOKEN") is None
    assert await repo.list_summaries("default", "github") == []


@pytest.mark.asyncio
async def test_delete_missing_is_noop(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.delete("default", "github", "DOES_NOT_EXIST")  # no raise


@pytest.mark.asyncio
async def test_delete_all_removes_every_secret_for_upstream(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "github", "A", "v" * 32)
    await repo.set("default", "github", "B", "v" * 32)
    await repo.set("default", "notion", "C", "v" * 32)
    await repo.delete_all("default", "github")
    assert await repo.list_summaries("default", "github") == []
    # Other upstream untouched.
    assert len(await repo.list_summaries("default", "notion")) == 1


@pytest.mark.asyncio
async def test_per_org_isolation(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("orgA", "github", "TOKEN", "value-a")
    await repo.set("orgB", "github", "TOKEN", "value-b")
    assert await repo.get_value("orgA", "github", "TOKEN") == "value-a"
    assert await repo.get_value("orgB", "github", "TOKEN") == "value-b"


@pytest.mark.asyncio
async def test_file_format_is_json_with_value_key(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set("default", "github", "TOKEN", "value-is-a-secret")
    raw = json.loads(repo.path.read_text())
    assert raw["default"]["github"]["TOKEN"]["value"] == "value-is-a-secret"


@pytest.mark.asyncio
async def test_plain_var_returns_value_in_summary(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set(
        "default", "github", "LOG_LEVEL", "debug", is_secret=False,
    )
    summaries = await repo.list_summaries("default", "github")
    assert summaries[0].is_secret is False
    assert summaries[0].value == "debug"


@pytest.mark.asyncio
async def test_replace_preserves_is_secret_flag(tmp_path: Path) -> None:
    """Pins the create-time-only contract: a secret stays a secret
    even when the replace call passes ``is_secret=False``."""
    repo = make_repo(tmp_path)
    await repo.set(
        "default", "github", "TOKEN", "value-1234567890", is_secret=True,
    )
    summary = await repo.set(
        "default", "github", "TOKEN", "rotated-value-1234567890",
        is_secret=False,  # caller's flag is ignored
    )
    assert summary.is_secret is True
    assert summary.value == "rotated-value-1234567890"


@pytest.mark.asyncio
async def test_replace_preserves_plain_flag(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.set(
        "default", "github", "LOG_LEVEL", "debug", is_secret=False,
    )
    summary = await repo.set(
        "default", "github", "LOG_LEVEL", "info",
        is_secret=True,  # caller's flag is ignored
    )
    assert summary.is_secret is False
    assert summary.value == "info"


@pytest.mark.asyncio
async def test_legacy_record_without_is_secret_field_reads_as_secret(
    tmp_path: Path,
) -> None:
    """Records written by v1 (no ``is_secret`` field) must default to
    ``is_secret=True`` on read so old data doesn't accidentally leak."""
    repo = make_repo(tmp_path)
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    repo.path.write_text(json.dumps({
        "default": {
            "github": {
                "LEGACY": {
                    "value": "value-from-v1",
                    "last_four": "rom1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                },
            },
        },
    }))
    summaries = await repo.list_summaries("default", "github")
    assert len(summaries) == 1
    assert summaries[0].is_secret is True
    # v1 records stored the plaintext under ``value`` already; the
    # new contract returns it for password rows too, so the legacy
    # row surfaces its plaintext through the SPA's reveal toggle.
    assert summaries[0].value == "value-from-v1"
