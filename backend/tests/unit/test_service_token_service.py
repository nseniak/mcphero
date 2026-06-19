from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.model.service_token import (
    SERVICE_TOKEN_PREFIX,
    ServiceTokenRecord,
    hash_service_token,
)
from mcpolis.domain.services.service_token_service import (
    ServiceTokenService,
)


class _RefusingRepo:
    """Repo double that fails the test if any method is called."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"repo.{name} must not be called")


class _CountingRepo:
    """Repo double that records ``get_by_hash`` lookups and never finds
    a record. Used to prove the prefix gate lets a hash lookup through
    (so the miss is a real registry miss, not a prefix short-circuit)
    while keeping the result None."""

    def __init__(self) -> None:
        self.hashes_looked_up: list[str] = []
        self.touch_calls: int = 0

    async def get_by_hash(self, token_hash: str) -> ServiceTokenRecord | None:
        self.hashes_looked_up.append(token_hash)
        return None

    async def touch_last_used(self, token_hash: str, when: datetime) -> None:
        self.touch_calls += 1


class _Clock:
    """Injectable wall + monotonic clock (house DI rule, no patching)."""

    def __init__(self) -> None:
        self.wall = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


def make_service(
    tmp_path: Path, clock: _Clock | None = None
) -> tuple[ServiceTokenService, FileServiceTokenRepository, _Clock]:
    repo = FileServiceTokenRepository(tmp_path)
    clock = clock or _Clock()
    service = ServiceTokenService(
        repo=repo, now=clock.now, monotonic=clock.monotonic,
    )
    return service, repo, clock


@pytest.mark.asyncio
async def test_mint_returns_prefixed_raw_token_and_stores_sha256_only(
    tmp_path: Path,
) -> None:
    service, repo, _ = make_service(tmp_path)
    minted = await service.mint(
        org_id="default",
        label="ci-bot",
        role_name="user",
        created_by="admin@example.com",
    )
    assert minted.raw_token.startswith(SERVICE_TOKEN_PREFIX)
    assert minted.record.token_hash == hash_service_token(minted.raw_token)
    # The raw token never reaches the registry file.
    on_disk = repo.path.read_text()
    assert minted.raw_token not in on_disk
    assert minted.record.token_hash in on_disk


@pytest.mark.asyncio
async def test_verify_valid_token_returns_record(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)
    minted = await service.mint(
        org_id="org-a",
        label="ci-bot",
        role_name="reader",
        created_by="admin@example.com",
    )
    record = await service.verify(minted.raw_token)
    assert record is not None
    assert record.org_id == "org-a"
    assert record.label == "ci-bot"
    assert record.role_name == "reader"


@pytest.mark.asyncio
async def test_verify_unknown_token_returns_none(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)
    assert await service.verify("svct_never-minted") is None


@pytest.mark.asyncio
async def test_verify_non_svct_prefix_returns_none_without_repo_lookup() -> None:
    service = ServiceTokenService(repo=_RefusingRepo())  # type: ignore[arg-type]
    assert await service.verify("some-oauth-access-token") is None


@pytest.mark.asyncio
async def test_verify_prefix_only_empty_secret_token_returns_none() -> None:
    """AUTH-2: ``svct_`` with no secret body passes the prefix gate
    (it *does* start with the prefix), so the registry is consulted —
    and the lookup misses, yielding None. The prefix gate is a router,
    not an authenticator: an empty-secret token is not special-cased,
    it just hashes to a value no record carries."""
    repo = _CountingRepo()
    service = ServiceTokenService(repo=repo)  # type: ignore[arg-type]
    assert await service.verify(SERVICE_TOKEN_PREFIX) is None
    # The prefix matched, so exactly one hash lookup happened — proving
    # the None is a real registry miss, not a prefix short-circuit.
    assert repo.hashes_looked_up == [hash_service_token(SERVICE_TOKEN_PREFIX)]
    # A missed lookup never touches last_used.
    assert repo.touch_calls == 0


@pytest.mark.asyncio
async def test_verify_throttles_last_used_writes(tmp_path: Path) -> None:
    service, repo, clock = make_service(tmp_path)
    minted = await service.mint(
        org_id="default",
        label="bot",
        role_name="user",
        created_by="admin@example.com",
    )

    # First verify writes last_used_at.
    await service.verify(minted.raw_token)
    first = await repo.get_by_hash(minted.record.token_hash)
    assert first is not None
    assert first.last_used_at == clock.wall

    # Second verify 30s later (within the 60s window): no new write.
    clock.mono += 30.0
    clock.wall = datetime(2026, 6, 1, 12, 0, 30, tzinfo=UTC)
    await service.verify(minted.raw_token)
    second = await repo.get_by_hash(minted.record.token_hash)
    assert second is not None
    assert second.last_used_at == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    # Third verify past the window: write happens.
    clock.mono += 61.0
    clock.wall = datetime(2026, 6, 1, 12, 2, tzinfo=UTC)
    await service.verify(minted.raw_token)
    third = await repo.get_by_hash(minted.record.token_hash)
    assert third is not None
    assert third.last_used_at == datetime(2026, 6, 1, 12, 2, tzinfo=UTC)


@pytest.mark.asyncio
async def test_revoke_then_verify_fails(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)
    minted = await service.mint(
        org_id="default",
        label="bot",
        role_name="user",
        created_by="admin@example.com",
    )
    assert await service.revoke("default", "bot") is True
    assert await service.verify(minted.raw_token) is None
    assert await service.revoke("default", "bot") is False


@pytest.mark.asyncio
async def test_list_for_org_returns_records(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)
    await service.mint(
        org_id="default", label="b-bot", role_name="user",
        created_by="admin@example.com",
    )
    await service.mint(
        org_id="default", label="a-bot", role_name="user",
        created_by="admin@example.com",
    )
    records = await service.list_for_org("default")
    assert [r.label for r in records] == ["a-bot", "b-bot"]


class _FakeOrgRepo:
    """Org repo double for the cascade test — deletion is cloud-only,
    so the file implementation refuses; only delete_organization is
    exercised here."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_organization(self, org_id: str) -> None:
        self.deleted.append(org_id)


@pytest.mark.asyncio
async def test_org_delete_cascade_revokes_tokens(tmp_path: Path) -> None:
    """Org deletion must kill the org's tokens — they bypass membership
    gating, so a survivor would keep working and be unrevocable."""
    from mcpolis.adapters.repositories.file_config_store import FileConfigStore
    from mcpolis.domain.services.org_service import OrgService

    service, repo, _ = make_service(tmp_path)
    minted = await service.mint(
        org_id="org-doomed", label="bot", role_name="user",
        created_by="admin@example.com",
    )
    other = await service.mint(
        org_id="org-alive", label="bot", role_name="user",
        created_by="admin@example.com",
    )
    fake_org_repo = _FakeOrgRepo()
    org_service = OrgService(
        org_repo=fake_org_repo,  # type: ignore[arg-type]
        config_repo=FileConfigStore(tmp_path / "config.json"),
        service_token_repo=repo,
    )
    await org_service.delete_organization("org-doomed")
    assert fake_org_repo.deleted == ["org-doomed"]
    assert await service.verify(minted.raw_token) is None
    assert await service.verify(other.raw_token) is not None
