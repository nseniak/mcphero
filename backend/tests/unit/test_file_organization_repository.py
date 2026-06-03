from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID


def make_repo(tmp_path: Path) -> FileOrganizationRepository:
    return FileOrganizationRepository(tmp_path)


@pytest.mark.asyncio
async def test_list_memberships_empty(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert await repo.list_memberships(DEFAULT_ORG_ID) == []


@pytest.mark.asyncio
async def test_add_and_list_membership(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.add_membership(DEFAULT_ORG_ID, "alice@x.com", "admin")
    rows = await repo.list_memberships(DEFAULT_ORG_ID)
    assert len(rows) == 1
    assert rows[0].email == "alice@x.com"
    assert rows[0].role == "admin"
    assert rows[0].org_id == DEFAULT_ORG_ID


@pytest.mark.asyncio
async def test_memberships_persist_across_instances(tmp_path: Path) -> None:
    repo1 = make_repo(tmp_path)
    await repo1.add_membership(DEFAULT_ORG_ID, "alice@x.com", "admin")
    await repo1.add_membership(DEFAULT_ORG_ID, "bob@x.com", "default")

    repo2 = make_repo(tmp_path)
    rows = await repo2.list_memberships(DEFAULT_ORG_ID)
    emails = {m.email for m in rows}
    assert emails == {"alice@x.com", "bob@x.com"}


@pytest.mark.asyncio
async def test_add_membership_is_idempotent_on_same_key(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    first = await repo.add_membership(DEFAULT_ORG_ID, "alice@x.com", "admin")
    second = await repo.add_membership(DEFAULT_ORG_ID, "alice@x.com", "default")

    rows = await repo.list_memberships(DEFAULT_ORG_ID)
    assert len(rows) == 1
    assert rows[0].role == "default"
    # created_at is preserved across role updates.
    assert second.created_at == first.created_at


@pytest.mark.asyncio
async def test_remove_membership(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.add_membership(DEFAULT_ORG_ID, "alice@x.com", "admin")
    await repo.remove_membership(DEFAULT_ORG_ID, "alice@x.com")
    assert await repo.list_memberships(DEFAULT_ORG_ID) == []

    # Reload from disk — the removal is persisted.
    repo2 = make_repo(tmp_path)
    assert await repo2.list_memberships(DEFAULT_ORG_ID) == []


@pytest.mark.asyncio
async def test_remove_missing_membership_is_noop(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.remove_membership(DEFAULT_ORG_ID, "nobody@x.com")
    assert await repo.list_memberships(DEFAULT_ORG_ID) == []


@pytest.mark.asyncio
async def test_get_memberships_for_email(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    await repo.add_membership(DEFAULT_ORG_ID, "alice@x.com", "admin")
    await repo.add_membership(DEFAULT_ORG_ID, "bob@x.com", "default")

    alice = await repo.get_memberships_for_email("alice@x.com")
    assert len(alice) == 1
    assert alice[0].email == "alice@x.com"

    nobody = await repo.get_memberships_for_email("nobody@x.com")
    assert nobody == []


@pytest.mark.asyncio
async def test_create_organization_raises(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(ValueError, match="standalone"):
        await repo.create_organization(slug="other", display_name="Other")


@pytest.mark.asyncio
async def test_list_organizations_returns_default_only(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    orgs = await repo.list_organizations()
    assert len(orgs) == 1
    assert orgs[0].id == DEFAULT_ORG_ID
    assert orgs[0].slug == "default"


@pytest.mark.asyncio
async def test_corrupt_memberships_file_starts_fresh(
    tmp_path: Path,
) -> None:
    (tmp_path / "memberships.json").write_text("not valid json {")
    repo = FileOrganizationRepository(tmp_path)
    assert await repo.list_memberships(DEFAULT_ORG_ID) == []

    # A subsequent add overwrites the corrupt file.
    await repo.add_membership(DEFAULT_ORG_ID, "alice@x.com", "admin")
    repo2 = FileOrganizationRepository(tmp_path)
    rows = await repo2.list_memberships(DEFAULT_ORG_ID)
    assert len(rows) == 1
