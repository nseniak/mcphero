from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from mcpolis.domain.model.upstream import (
    DiscoveredPrompt,
    DiscoveredResource,
    DiscoveredResourceTemplate,
    DiscoveredTool,
)


class ToolCatalogSnapshot(BaseModel):
    """Per-upstream catalog of tools / resources / prompts.

    Persisted by ``ToolCatalogRepository`` so the admin permissions
    UI can render the upstream's tool list across backend restarts
    without requiring an OAuth reconnect first. Hydration is per-org
    on first runtime access; write-through happens on
    ``ToolRegistry.refresh_upstream``.
    """

    tools: list[DiscoveredTool] = Field(default_factory=list[DiscoveredTool])
    resources: list[DiscoveredResource] = Field(
        default_factory=list[DiscoveredResource],
    )
    resource_templates: list[DiscoveredResourceTemplate] = Field(
        default_factory=list[DiscoveredResourceTemplate],
    )
    prompts: list[DiscoveredPrompt] = Field(
        default_factory=list[DiscoveredPrompt],
    )


class ToolCatalogRepository(Protocol):
    async def load_all(
        self, org_id: str,
    ) -> dict[str, ToolCatalogSnapshot]:
        """Return per-upstream catalog snapshot for the org.

        Keyed by ``upstream_id``. Returns an empty dict if no catalog
        has been persisted yet.
        """
        ...

    async def upsert_upstream(
        self,
        org_id: str,
        upstream_id: str,
        snapshot: ToolCatalogSnapshot,
    ) -> None:
        """Replace the persisted snapshot for one upstream."""
        ...

    async def delete_upstream(
        self, org_id: str, upstream_id: str,
    ) -> None:
        """Remove the persisted snapshot for one upstream."""
        ...

    async def delete_all_for_org(self, org_id: str) -> None:
        """Remove every persisted catalog snapshot for the org
        (org-deletion cascade). Idempotent."""
        ...
