"""In-memory ``SandboxPersistenceRepository`` impl.

Used in standalone mode (no Mongo) and as the default test double.
Storage is a dict keyed by ``(org_id, upstream_id)``; values are
returned verbatim (no encryption, no filtering).

Standalone mode tradeoff: refs don't survive a process restart, so
the resilience guarantees described in plan §"Resilience" do NOT
apply to standalone deployments. That's intentional — the
standalone configuration explicitly accepts in-memory state for the
sake of zero-infra dev. Cloud mode wires in the Mongo impl.
"""
from __future__ import annotations

import asyncio

from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
    SandboxPersistenceRepository,
)


class InMemorySandboxPersistenceRepository(SandboxPersistenceRepository):
    """Dict-backed sandbox persistence."""

    def __init__(self) -> None:
        self._refs: dict[tuple[str, str], SandboxPersistedRef] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, ref: SandboxPersistedRef) -> None:
        async with self._lock:
            self._refs[(ref.org_id, ref.upstream_id)] = ref

    async def get(
        self, *, org_id: str, upstream_id: str,
    ) -> SandboxPersistedRef | None:
        async with self._lock:
            return self._refs.get((org_id, upstream_id))

    async def delete(self, *, org_id: str, upstream_id: str) -> None:
        async with self._lock:
            self._refs.pop((org_id, upstream_id), None)

    async def list_for_org(
        self, *, org_id: str,
    ) -> list[SandboxPersistedRef]:
        async with self._lock:
            return [
                ref for (oid, _), ref in self._refs.items()
                if oid == org_id
            ]

    async def list_all_unscoped(self) -> list[SandboxPersistedRef]:
        async with self._lock:
            return list(self._refs.values())


__all__ = ["InMemorySandboxPersistenceRepository"]
