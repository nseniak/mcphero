"""Sandbox persistence round-trips through both backends.

Same parametrize-over-backends shape as
``test_parametrized_egress_denylist`` — covers ``InMemory`` and the
Mongo impl, when Mongo is reachable.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.repositories.mongo_client import (
    COLL_SANDBOX_REFS,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_sandbox_persistence_repository import (
    MongoSandboxPersistenceRepository,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
    SandboxPersistenceRepository,
)
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database

BACKENDS: list[str] = ["inmemory"] + (["mongo"] if mongo_available() else [])


@asynccontextmanager
async def _make_repo(
    backend: str,
) -> AsyncIterator[SandboxPersistenceRepository]:
    if backend == "inmemory":
        yield InMemorySandboxPersistenceRepository()
        return
    async with temp_mongo_database() as db:
        coll = OrgScopedCollection(
            db[COLL_SANDBOX_REFS], COLL_SANDBOX_REFS,
        )
        yield MongoSandboxPersistenceRepository(coll)


def make_ref(
    *,
    org_id: str = "acme",
    upstream_id: str = "ups-1",
    provider: str = "e2b",
    sandbox_id: str | None = "sbx-abc",
    paused_snapshot_id: str | None = None,
    instance: str = "instance-1",
    metadata: dict[str, str] | None = None,
) -> SandboxPersistedRef:
    return SandboxPersistedRef(
        provider=provider,  # type: ignore[arg-type]
        org_id=org_id,
        upstream_id=upstream_id,
        mcpolis_instance=instance,
        sandbox_id=sandbox_id,
        paused_snapshot_id=paused_snapshot_id,
        pid=None,
        metadata=metadata or {},
        cached_server_info=None,
        cached_self_description=None,
        last_updated=datetime.now(tz=timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_get_returns_none_when_missing(backend: str) -> None:
    async with _make_repo(backend) as repo:
        result = await repo.get(org_id="acme", upstream_id="missing")
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_upsert_and_get_round_trip(backend: str) -> None:
    async with _make_repo(backend) as repo:
        ref = make_ref(metadata={"template": "mcpolis-node-cpu1-ram1024"})
        await repo.upsert(ref)
        loaded = await repo.get(org_id=ref.org_id, upstream_id=ref.upstream_id)
        assert loaded is not None
        assert loaded.provider == ref.provider
        assert loaded.org_id == ref.org_id
        assert loaded.upstream_id == ref.upstream_id
        assert loaded.sandbox_id == "sbx-abc"
        assert loaded.paused_snapshot_id is None
        assert loaded.mcpolis_instance == "instance-1"
        assert loaded.metadata == {"template": "mcpolis-node-cpu1-ram1024"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_upsert_overwrites_in_place(backend: str) -> None:
    async with _make_repo(backend) as repo:
        # Seed with a live sandbox.
        live = make_ref(sandbox_id="sbx-1", paused_snapshot_id=None)
        await repo.upsert(live)
        # Pause: same key, sandbox cleared, snapshot set.
        paused = make_ref(sandbox_id=None, paused_snapshot_id="snap-1")
        await repo.upsert(paused)
        loaded = await repo.get(org_id=live.org_id, upstream_id=live.upstream_id)
        assert loaded is not None
        assert loaded.sandbox_id is None
        assert loaded.paused_snapshot_id == "snap-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_delete_removes_ref(backend: str) -> None:
    async with _make_repo(backend) as repo:
        ref = make_ref()
        await repo.upsert(ref)
        await repo.delete(org_id=ref.org_id, upstream_id=ref.upstream_id)
        result = await repo.get(
            org_id=ref.org_id, upstream_id=ref.upstream_id,
        )
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_delete_missing_is_noop(backend: str) -> None:
    async with _make_repo(backend) as repo:
        # Must not raise.
        await repo.delete(org_id="nope", upstream_id="nada")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_list_for_org_filters_by_org(backend: str) -> None:
    async with _make_repo(backend) as repo:
        await repo.upsert(make_ref(org_id="acme", upstream_id="a"))
        await repo.upsert(make_ref(org_id="acme", upstream_id="b"))
        await repo.upsert(make_ref(org_id="other", upstream_id="a"))
        acme = await repo.list_for_org(org_id="acme")
        assert len(acme) == 2
        ids = {r.upstream_id for r in acme}
        assert ids == {"a", "b"}
        for r in acme:
            assert r.org_id == "acme"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_list_all_unscoped_crosses_orgs(backend: str) -> None:
    async with _make_repo(backend) as repo:
        await repo.upsert(make_ref(org_id="acme", upstream_id="a"))
        await repo.upsert(make_ref(org_id="other", upstream_id="b"))
        all_refs = await repo.list_all_unscoped()
        assert len(all_refs) == 2
        orgs = {r.org_id for r in all_refs}
        assert orgs == {"acme", "other"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_paused_only_ref_round_trips(backend: str) -> None:
    """A ref with no live ``sandbox_id`` (paused-only) must round-trip
    cleanly — Mongo's BSON null encoding shouldn't drop the field."""
    async with _make_repo(backend) as repo:
        ref = make_ref(
            sandbox_id=None,
            paused_snapshot_id="snap-only",
        )
        await repo.upsert(ref)
        loaded = await repo.get(
            org_id=ref.org_id, upstream_id=ref.upstream_id,
        )
        assert loaded is not None
        assert loaded.sandbox_id is None
        assert loaded.paused_snapshot_id == "snap-only"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_creating_marker_both_none_ref_round_trips(backend: str) -> None:
    """The E2B in-flight-create "creating" marker is a ref with BOTH
    ``sandbox_id`` and ``paused_snapshot_id`` None — a state the normal
    lifecycle never produces, which the reconciler uses as its
    in-flight discriminator. It must round-trip cleanly (the Mongo strict
    deserializer must NOT reject a both-None ref as malformed), or BUG-2's
    fix silently breaks on the cloud backend."""
    async with _make_repo(backend) as repo:
        marker = make_ref(sandbox_id=None, paused_snapshot_id=None)
        await repo.upsert(marker)
        loaded = await repo.get(
            org_id=marker.org_id, upstream_id=marker.upstream_id,
        )
        assert loaded is not None
        assert loaded.sandbox_id is None
        assert loaded.paused_snapshot_id is None
        assert loaded.mcpolis_instance == marker.mcpolis_instance


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_instance_field_round_trips(backend: str) -> None:
    """``mcpolis_instance`` is the multi-instance safety key — it must
    round-trip exactly so the reconciler can distinguish "my refs"
    from "another instance's refs"."""
    async with _make_repo(backend) as repo:
        ref = make_ref(instance="instance-blue-7")
        await repo.upsert(ref)
        loaded = await repo.get(
            org_id=ref.org_id, upstream_id=ref.upstream_id,
        )
        assert loaded is not None
        assert loaded.mcpolis_instance == "instance-blue-7"
