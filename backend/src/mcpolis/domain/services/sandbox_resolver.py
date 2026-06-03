"""Pick the sandbox provider for a given org's stdio MCP session.

Day-one implementation reads a single global value (set on the
backend at startup from ``MCPOLIS_SANDBOX_PROVIDER``) and ignores
``org_id``. The async + ``org_id``-keyed signature is in place from
day one so the per-org override (which lands on
``Org.sandbox_provider`` in Mongo) is a swap of this one method, no
call-site changes anywhere else in the codebase.

See ``internal/plans/currently-mcpolis-runs-stdio-witty-ocean.md`` §"Provider
selection: global today, per-org later" for the rationale.
"""
from __future__ import annotations

from mcpolis.domain.services.sandbox_service import SandboxProviderName


class SandboxResolver:
    """Resolves an ``org_id`` to a sandbox provider name.

    A *resolver* (not a registry) — it doesn't know about concrete
    services. The dict ``{name → SandboxService}`` lives on
    ``UpstreamClientManager``; this layer only decides which key to
    look up.
    """

    def __init__(self, *, global_provider: SandboxProviderName) -> None:
        self._global_provider: SandboxProviderName = global_provider

    async def resolve(self, *, org_id: str) -> SandboxProviderName:
        """Return the provider name for ``org_id``.

        Day-one: ignores ``org_id`` and returns the global default.
        The future per-org swap reads ``Org.sandbox_provider`` first,
        falls back to ``self._global_provider``.
        """
        _ = org_id
        return self._global_provider


__all__ = ["SandboxResolver"]
