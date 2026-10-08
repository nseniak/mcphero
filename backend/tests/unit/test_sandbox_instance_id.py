"""The sandbox instance id must survive a restart.

The boot reconciler lists E2B sandboxes by their ``mcpolis_instance``
tag. When that tag was a fresh random id per process, a new process
could never list what an old one left behind, so the orphan cleanup
never killed anything. The id now lives in the sandbox-ref store, and
the lifespan swaps it in before any MCP connects.
"""
from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from mcpolis.adapters.sandbox_e2b import E2BSandboxReconciler, E2BSandboxService
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.sandbox_service import (
    SandboxProviderName,
    SandboxResources,
    SandboxService,
)
from mcpolis.entrypoints import app as app_module
from mcpolis.entrypoints.app import _adopt_stable_sandbox_instance  # pyright: ignore[reportPrivateUsage]
from tests.unit.factories import make_upstream_definition
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database
from tests.unit.sandbox_e2b_mock import MockE2BClient, make_mock_e2b_client

needs_mongo = pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")


class _FakeRuntimeManager:
    def __init__(self) -> None:
        self.adopted: list[str] = []

    def adopt_instance_id(self, instance_id: str) -> None:
        self.adopted.append(instance_id)


class _Storage:
    def __init__(self, persistence: SandboxPersistenceRepository) -> None:
        self.sandbox_persistence_repo = persistence


def make_service(
    client: MockE2BClient, persistence: SandboxPersistenceRepository,
) -> E2BSandboxService:
    return E2BSandboxService(
        client,
        mcpolis_instance="provisional",
        on_timeout_seconds=60,
        persistence=persistence,
        reuse_sandboxes_on_restart=True,  # the production default
    )


def make_resources() -> SandboxResources:
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


@asynccontextmanager
async def make_mongo_repo_pair() -> AsyncIterator[
    tuple[MongoSandboxPersistenceRepository, MongoSandboxPersistenceRepository]
]:
    """Two repo objects on one database: two processes, one environment."""
    async with temp_mongo_database() as db:
        yield (
            MongoSandboxPersistenceRepository(
                OrgScopedCollection(db[COLL_SANDBOX_REFS], COLL_SANDBOX_REFS),
            ),
            MongoSandboxPersistenceRepository(
                OrgScopedCollection(db[COLL_SANDBOX_REFS], COLL_SANDBOX_REFS),
            ),
        )


async def boot(
    client: MockE2BClient, persistence: SandboxPersistenceRepository,
) -> tuple[E2BSandboxService, str, _FakeRuntimeManager]:
    """What the lifespan does before any MCP connects."""
    service = make_service(client, persistence)
    runtime_manager = _FakeRuntimeManager()
    services: dict[SandboxProviderName, SandboxService] = {"e2b": service}
    instance = await _adopt_stable_sandbox_instance(
        storage=_Storage(persistence),  # type: ignore[arg-type]
        sandbox_services=services,
        runtime_manager=runtime_manager,  # type: ignore[arg-type]
        provisional="provisional",
    )
    return service, instance, runtime_manager


async def open_preserved_session(service: E2BSandboxService) -> None:
    """A session left running across shutdown, as on a deploy."""
    async with service.session(
        session_id="s1",
        org_id="acme",
        upstream=make_upstream_definition(id="ups-x", command="npx"),
        resources=make_resources(),
        denylist=(),
    ):
        service.mark_session_preserve_on_close("s1")


@needs_mongo
@pytest.mark.asyncio
async def test_mongo_instance_id_is_the_same_for_every_process() -> None:
    async with make_mongo_repo_pair() as (first, second):
        a = await first.get_or_create_instance_id()
        assert a
        assert await first.get_or_create_instance_id() == a
        assert await second.get_or_create_instance_id() == a


@pytest.mark.asyncio
async def test_inmemory_instance_id_is_stable_for_its_lifetime() -> None:
    repo = InMemorySandboxPersistenceRepository()
    assert await repo.get_or_create_instance_id() == (
        await repo.get_or_create_instance_id()
    )
    assert await repo.get_or_create_instance_id() != (
        await InMemorySandboxPersistenceRepository().get_or_create_instance_id()
    )


@needs_mongo
@pytest.mark.asyncio
async def test_second_boot_finds_what_the_first_left() -> None:
    client = make_mock_e2b_client()  # one E2B account across both boots
    async with make_mongo_repo_pair() as (store_1, store_2):
        service_1, instance_1, runtimes_1 = await boot(client, store_1)
        await open_preserved_session(service_1)
        assert client.creates[0].metadata["mcpolis_instance"] == instance_1
        assert runtimes_1.adopted == [instance_1]

        _, instance_2, _ = await boot(client, store_2)
        assert instance_2 == instance_1
        reconciler = E2BSandboxReconciler(
            client, store_2, mcpolis_instance=instance_2,
        )

        # The preserved sandbox has a ref: the next connect reuses it.
        report = await reconciler.reconcile()
        assert report.kept_tracked_sandboxes == 1
        assert report.killed_orphan_sandboxes == 0
        assert client.kills == []

        # Same sandbox without a ref (a crash before the ref was written)
        # is an orphan, and boot 2 now sees it.
        await store_2.delete(org_id="acme", upstream_id="ups-x")
        report = await reconciler.reconcile()
        assert report.killed_orphan_sandboxes == 1
        assert len(client.kills) == 1
        assert client.live_infos == []


@pytest.mark.asyncio
async def test_standalone_keeps_the_provisional_id() -> None:
    """In-memory refs die with the process; nothing to adopt."""
    client = make_mock_e2b_client()
    _, instance, runtimes = await boot(
        client, InMemorySandboxPersistenceRepository(),
    )
    assert instance == "provisional"
    assert runtimes.adopted == []


@pytest.mark.asyncio
async def test_adopt_refused_after_a_session_started() -> None:
    """That session's sandbox and ref already carry the old id."""
    client = make_mock_e2b_client()
    service = make_service(client, InMemorySandboxPersistenceRepository())
    await open_preserved_session(service)
    with pytest.raises(RuntimeError):
        service.adopt_instance_id("stable")


def test_lifespan_adopts_the_id_before_cleanup_and_before_connecting() -> None:
    """Order inside the app lifespan. A source-order check, deliberately
    crude: driving a full cloud lifespan needs Mongo, Redis and E2B
    fakes. Adopting after the reconcile would list sandboxes by the
    throwaway id (finding nothing); adopting after the connects would
    raise, because sessions already started under the old id."""
    source = inspect.getsource(app_module.create_app)
    lifespan = source[source.index("async def app_lifespan"):]
    positions = [
        lifespan.index("await initialize_storage("),
        lifespan.index("await _adopt_stable_sandbox_instance("),
        lifespan.index("wipe_for_fresh_restart()"),
        lifespan.index("await _run_sandbox_reconcile_at_boot("),
        lifespan.index("_connect_all_orgs_background()"),
    ]
    assert positions == sorted(positions)
