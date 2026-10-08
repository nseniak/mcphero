"""Org deletion must kill the org's E2B sandboxes, not just forget them.

A sandbox with a live session is killed by the runtime teardown (its
session closes without preserve). A PAUSED sandbox has no session — the
upstream sits in DEFERRED_ATTACH, or the session was torn down with
preserve after E2B paused it — so only the persisted ref knows about it.
Purging that ref without killing the sandbox leaves it on E2B, billed,
with nothing left that points at it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxReconciler, E2BSandboxService
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.service import (
    VOLUME_METADATA_KEY,
    sandboxes_to_kill,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
)
from mcpolis.domain.services.org_service import OrgService
from tests.unit.sandbox_e2b_mock import (
    MockE2BClient,
    MockE2BSandboxInfo,
    make_mock_e2b_client,
)


class _FakeOrgRepo:
    """Deletion is cloud-only (the file repo refuses); only
    ``delete_organization`` is exercised here."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_organization(self, org_id: str) -> None:
        self.deleted.append(org_id)


def make_ref(
    *, org_id: str, upstream_id: str, sandbox_id: str, volume_id: str | None,
) -> SandboxPersistedRef:
    return SandboxPersistedRef(
        provider="e2b",
        org_id=org_id,
        upstream_id=upstream_id,
        mcpolis_instance="inst-1",
        sandbox_id=sandbox_id,
        paused_snapshot_id=None,
        pid=7,
        metadata={VOLUME_METADATA_KEY: volume_id} if volume_id else {},
        cached_server_info=None,
        cached_self_description=None,
        last_updated=datetime(2026, 1, 1, tzinfo=UTC),
    )


def make_paused_sandbox(client: MockE2BClient, sandbox_id: str) -> None:
    client.live_infos.append(MockE2BSandboxInfo(
        sandbox_id=sandbox_id,
        state="paused",
        metadata={"mcpolis_instance": "inst-1"},
        created_at=None,
    ))


def make_org_service(
    tmp_path: Path,
    persistence: InMemorySandboxPersistenceRepository,
    client: MockE2BClient,
) -> tuple[OrgService, _FakeOrgRepo]:
    org_repo = _FakeOrgRepo()
    service = E2BSandboxService(
        client,
        mcpolis_instance="inst-1",
        persistence=persistence,
        on_timeout_seconds=60,
        volumes_enabled=True,
    )
    org_service = OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=FileConfigStore(tmp_path / "config.json"),
        sandbox_persistence_repo=persistence,
        sandbox_services={"e2b": service},
    )
    return org_service, org_repo


@pytest.mark.asyncio
async def test_org_delete_kills_paused_sandboxes_and_their_volumes(
    tmp_path: Path,
) -> None:
    persistence = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    await persistence.upsert(make_ref(
        org_id="org-doomed", upstream_id="github",
        sandbox_id="sbx-paused-1", volume_id="vol-1",
    ))
    await persistence.upsert(make_ref(
        org_id="org-doomed", upstream_id="slack",
        sandbox_id="sbx-paused-2", volume_id=None,
    ))
    await persistence.upsert(make_ref(
        org_id="org-alive", upstream_id="github",
        sandbox_id="sbx-bystander", volume_id="vol-bystander",
    ))
    for sid in ("sbx-paused-1", "sbx-paused-2", "sbx-bystander"):
        make_paused_sandbox(client, sid)
    org_service, org_repo = make_org_service(tmp_path, persistence, client)

    await org_service.delete_organization("org-doomed")

    assert org_repo.deleted == ["org-doomed"]
    killed = {k.sandbox_id for k in client.kills}
    assert killed == {"sbx-paused-1", "sbx-paused-2"}
    assert [d.volume_id for d in client.volume_destroys] == ["vol-1"]
    assert await persistence.list_for_org(org_id="org-doomed") == []
    bystander = await persistence.list_for_org(org_id="org-alive")
    assert [r.sandbox_id for r in bystander] == ["sbx-bystander"]


@pytest.mark.asyncio
async def test_org_delete_still_purges_refs_when_a_volume_destroy_fails(
    tmp_path: Path,
) -> None:
    """A failing E2B call, here an error the E2B client does not even
    wrap, must not strand the rest of the cascade: the sandbox is still
    killed and the ref still purged."""
    persistence = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    client.volume_destroy_raises = RuntimeError("E2B is down")
    await persistence.upsert(make_ref(
        org_id="org-doomed", upstream_id="github",
        sandbox_id="sbx-paused-1", volume_id="vol-1",
    ))
    make_paused_sandbox(client, "sbx-paused-1")
    org_service, _ = make_org_service(tmp_path, persistence, client)

    await org_service.delete_organization("org-doomed")

    assert {k.sandbox_id for k in client.kills} == {"sbx-paused-1"}
    assert await persistence.list_for_org(org_id="org-doomed") == []


@pytest.mark.asyncio
async def test_org_delete_continues_past_a_failing_kill(tmp_path: Path) -> None:
    """One sandbox that cannot be killed must not stop the others, nor
    the purge of the rest. Its ref stays, naming only the sandbox still
    to kill, for the next boot's reconcile."""
    persistence = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    client.kill_raises = E2BSDKError("RateLimit", "E2B says slow down")
    for upstream_id, sid in (("github", "sbx-1"), ("slack", "sbx-2")):
        await persistence.upsert(make_ref(
            org_id="org-doomed", upstream_id=upstream_id,
            sandbox_id=sid, volume_id=None,
        ))
        make_paused_sandbox(client, sid)
    org_service, org_repo = make_org_service(tmp_path, persistence, client)

    await org_service.delete_organization("org-doomed")

    assert org_repo.deleted == ["org-doomed"]
    assert {k.sandbox_id for k in client.kills} == {"sbx-1", "sbx-2"}
    assert await sandboxes_named_to_kill(persistence, "org-doomed") == {
        "github": ["sbx-1"], "slack": ["sbx-2"],
    }


def refuse_kills_of(client: MockE2BClient, refused: str) -> None:
    """E2B refuses to kill ``refused``; other kills go through."""
    kill = client.kill_sandbox

    async def kill_sandbox(sandbox_id: str) -> None:
        if sandbox_id == refused:
            raise E2BSDKError("E2BSDKError", "502 Bad Gateway")
        await kill(sandbox_id)

    client.kill_sandbox = kill_sandbox  # type: ignore[method-assign]


async def sandboxes_named_to_kill(
    persistence: InMemorySandboxPersistenceRepository, org_id: str,
) -> dict[str, list[str]]:
    """The org's refs left after its deletion: the sandboxes each names
    as still to kill. None of them names a sandbox to reuse."""
    refs = await persistence.list_for_org(org_id=org_id)
    assert [ref.sandbox_id for ref in refs] == [None] * len(refs)
    return {ref.upstream_id: sandboxes_to_kill(ref) for ref in refs}


@pytest.mark.asyncio
async def test_a_sandbox_an_org_delete_could_not_kill_is_killed_at_the_next_boot(
    tmp_path: Path,
) -> None:
    """The org's sandbox was made before the instance id became one value
    per database, so its tag is one the boot reconcile does not list, and
    E2B refuses its kill. Purged with the org, its ref was the last thing
    that named it: it stayed on the account for good. The ref stays,
    listing only that sandbox, the next boot's reconcile kills it whatever
    its tag, and the ref goes; the org's other refs are purged as usual."""
    persistence = InMemorySandboxPersistenceRepository()
    client = make_mock_e2b_client()
    client.live_infos.append(MockE2BSandboxInfo(
        sandbox_id="sbx-old",
        state="paused",
        metadata={"mcpolis_instance": "per-process-1f3a"},
    ))
    make_paused_sandbox(client, "sbx-new")
    for upstream_id, sid in (("github", "sbx-old"), ("slack", "sbx-new")):
        await persistence.upsert(make_ref(
            org_id="org-doomed", upstream_id=upstream_id,
            sandbox_id=sid, volume_id=None,
        ))
    refuse_kills_of(client, "sbx-old")
    org_service, _ = make_org_service(tmp_path, persistence, client)

    await org_service.delete_organization("org-doomed")
    left_by_the_deletion = await sandboxes_named_to_kill(persistence, "org-doomed")
    del client.kill_sandbox  # E2B kills it again
    report = await E2BSandboxReconciler(
        client, persistence, mcpolis_instance="inst-1",
    ).reconcile()

    assert left_by_the_deletion == {"github": ["sbx-old"]}
    assert [info.sandbox_id for info in client.live_infos] == [], report
    assert await persistence.list_for_org(org_id="org-doomed") == []
