from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit.factories import make_audit_entry


@pytest.mark.asyncio
async def test_log_writes_jsonl_line(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    service = FileAuditRepository(log_path)
    entry = make_audit_entry(user_id="alice", tool="github__create_issue")
    await service.log(DEFAULT_ORG_ID,entry)

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["user_id"] == "alice"
    assert data["tool"] == "github__create_issue"


@pytest.mark.asyncio
async def test_log_appends_multiple_entries(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    service = FileAuditRepository(log_path)
    await service.log(DEFAULT_ORG_ID,make_audit_entry(user_id="alice"))
    await service.log(DEFAULT_ORG_ID,make_audit_entry(user_id="bob"))

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["user_id"] == "alice"
    assert json.loads(lines[1])["user_id"] == "bob"


@pytest.mark.asyncio
async def test_search_across_rotated_files(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    # Simulate a rotated file from a previous day
    rotated = tmp_path / "audit.jsonl.2026-04-01"
    old_entry = make_audit_entry(user_id="old_user", timestamp="2026-04-01T10:00:00Z")
    rotated.write_text(old_entry.model_dump_json() + "\n")

    service = FileAuditRepository(log_path)
    await service.log(DEFAULT_ORG_ID,make_audit_entry(user_id="new_user", timestamp="2026-04-02T10:00:00Z"))

    results = await service.search(DEFAULT_ORG_ID,limit=10)
    user_ids = [r["user_id"] for r in results]
    # Current file entries come first (newest), then rotated file entries
    assert user_ids == ["new_user", "old_user"]


@pytest.mark.asyncio
async def test_search_rotated_files_respects_filters(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    rotated = tmp_path / "audit.jsonl.2026-04-01"
    rotated.write_text(
        make_audit_entry(user_id="alice", timestamp="2026-04-01T10:00:00Z").model_dump_json()
        + "\n"
        + make_audit_entry(user_id="bob", timestamp="2026-04-01T11:00:00Z").model_dump_json()
        + "\n"
    )

    service = FileAuditRepository(log_path)
    await service.log(DEFAULT_ORG_ID,make_audit_entry(user_id="alice", timestamp="2026-04-02T10:00:00Z"))

    results = await service.search(DEFAULT_ORG_ID,user_id="alice", limit=10)
    assert len(results) == 2
    assert all(r["user_id"] == "alice" for r in results)


@pytest.mark.asyncio
async def test_get_filter_values_across_rotated_files(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    rotated = tmp_path / "audit.jsonl.2026-04-01"
    rotated.write_text(
        make_audit_entry(user_id="old_user", upstream_id="mcp-a").model_dump_json() + "\n"
    )

    service = FileAuditRepository(log_path)
    await service.log(DEFAULT_ORG_ID,make_audit_entry(user_id="new_user", upstream_id="mcp-b"))

    filters = await service.get_filter_values(DEFAULT_ORG_ID)
    assert "old_user" in filters["user_ids"]
    assert "new_user" in filters["user_ids"]
    assert "mcp-a" in filters["upstream_ids"]
    assert "mcp-b" in filters["upstream_ids"]
