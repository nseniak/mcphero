from __future__ import annotations

from typing import Any, Protocol

from mcpolis.domain.model.upstream import UpstreamDefinition


class UpstreamConfigRepository(Protocol):
    async def get_all(self, org_id: str) -> list[UpstreamDefinition]: ...

    async def get(self, org_id: str, upstream_id: str) -> UpstreamDefinition | None: ...

    async def add(self, org_id: str, upstream: UpstreamDefinition) -> None: ...

    async def update(self, org_id: str, upstream: UpstreamDefinition) -> None: ...

    async def update_server_config(
        self, org_id: str, upstream_id: str, server_config: dict[str, Any]
    ) -> None: ...

    async def remove(self, org_id: str, upstream_id: str) -> None: ...
