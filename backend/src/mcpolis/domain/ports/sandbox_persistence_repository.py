"""Port for durable storage of sandbox refs.

Backs the in-memory ``SandboxStateRegistry`` with a write-through so a
backend crash doesn't leak the ``(org_id, upstream_id) → {sandbox_id,
paused_snapshot_id}`` mapping. Per-backend startup reconcilers also
read from this port to figure out which sandboxes / snapshots they
recognize vs. which need cleanup.

See ``internal/plans/currently-mcpolis-runs-stdio-witty-ocean.md``
§"Resilience" for the rationale.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamSelfDescription,
)
from mcpolis.domain.services.sandbox_service import SandboxProviderName


class MalformedRefError(Exception):
    """Raised by the persistence-layer deserializer when a stored
    ``sandbox_refs`` document is missing a required field or has a
    value of the wrong type.

    Boundary callers (``get`` / ``list_for_org`` / ``list_all_unscoped``)
    catch this, log at WARNING with ``field`` + ``reason``, and treat
    the ref as missing — i.e. the upstream lands in the FAILED state
    of the manager's state machine on first read.

    Expected during the migration window after the Phase E persistence
    consolidation ships; not pageable. After the migration script
    runs, the steady-state count of these warnings should be zero.
    """

    def __init__(self, *, field: str, reason: str) -> None:
        super().__init__(f"malformed sandbox_ref doc: {field}: {reason}")
        self.field = field
        self.reason = reason


class SandboxPersistedRef(BaseModel):
    """One sandbox / snapshot mapping for an ``(org, upstream)`` pair.

    Either ``sandbox_id`` or ``paused_snapshot_id`` (or both) is set
    at any moment:

    - **Live sandbox**: ``sandbox_id`` populated, ``paused_snapshot_id``
      ``None``.
    - **Paused snapshot**: ``paused_snapshot_id`` populated,
      ``sandbox_id`` ``None``.
    - **Pause-in-flight**: both populated transiently while the pause
      verb returns and the live ref is being cleared.

    ``mcpolis_instance`` carries the backend-process UUID minted at
    startup so reconcilers can distinguish "my sandboxes" from another
    instance's (multi-instance / blue-green safety, see plan §
    Resilience).

    Phase E note: every field is required at construction. Domain
    nullability stays where lifecycle states allow (``sandbox_id``,
    ``paused_snapshot_id``, ``pid``, ``cached_*``) but callers must
    pass an explicit ``None`` rather than relying on a default. The
    strict shape is paired with a strict Mongo deserializer (see
    ``MalformedRefError``).
    """

    model_config = ConfigDict(frozen=True)

    provider: SandboxProviderName
    org_id: str = Field(min_length=1)
    upstream_id: str = Field(min_length=1)
    mcpolis_instance: str = Field(min_length=1)

    sandbox_id: str | None
    paused_snapshot_id: str | None

    # Process id of the long-lived MCP subprocess **inside** the
    # sandbox. Set when ``sandbox_id`` is populated AND a
    # ``run_command`` was issued; used at next-boot reconnect to
    # call ``commands.connect(pid)`` and re-establish the streaming
    # RPC without restarting the MCP process. ``None`` for refs
    # that only carry a paused-snapshot id.
    pid: int | None

    # Free-form per-provider metadata (e.g. E2B template id used at
    # create time). Persisted alongside the ref; not interpreted by
    # the registry layer. Empty dict when no metadata applies — the
    # field itself is always present.
    metadata: dict[str, str]

    # Cached MCP ``initialize`` result. Lets the dashboard render
    # upstream cards (server name/version, instructions, capabilities
    # extensions) WITHOUT opening a live shared session at boot —
    # which would wake every paused sandbox via E2B's auto_resume.
    # Refreshed on every successful ``connect_shared`` so the cache
    # tracks the live MCP. ``None`` for upstreams that have never
    # connected.
    cached_server_info: ServerInfo | None
    cached_self_description: UpstreamSelfDescription | None

    last_updated: datetime


class SandboxPersistenceRepository(Protocol):
    """Durable storage for sandbox refs.

    Two implementations: in-memory (standalone mode + tests) and
    Mongo-backed (cloud mode). Both must produce the same observable
    behaviour for the parameterized test suite.
    """

    async def upsert(self, ref: SandboxPersistedRef) -> None:
        """Write-through a ref. Idempotent — same ``(org_id,
        upstream_id)`` overwrites previous content, including
        clearing ``sandbox_id`` / ``paused_snapshot_id`` to ``None``."""
        ...

    async def get(
        self, *, org_id: str, upstream_id: str,
    ) -> SandboxPersistedRef | None:
        """Read a single ref. Returns ``None`` when no ref exists."""
        ...

    async def delete(self, *, org_id: str, upstream_id: str) -> None:
        """Remove a ref. No-op when the ref didn't exist."""
        ...

    async def delete_all_for_org(self, *, org_id: str) -> int:
        """Remove every ref for the org, across all upstreams
        (org-deletion cascade). Returns the count removed. Idempotent."""
        ...

    async def list_for_org(self, *, org_id: str) -> list[SandboxPersistedRef]:
        """Return every ref for a single org. Used by org-scoped
        reconciliation paths."""
        ...

    async def list_all_unscoped(self) -> list[SandboxPersistedRef]:
        """Return every persisted ref across every org.

        **Operator / reconciler-only.** Per-backend startup reconcilers
        need a global view to cross-reference against
        ``Sandbox.list()`` from the provider; user-facing code should
        never reach this.
        """
        ...


__all__ = [
    "MalformedRefError",
    "SandboxPersistedRef",
    "SandboxPersistenceRepository",
]
