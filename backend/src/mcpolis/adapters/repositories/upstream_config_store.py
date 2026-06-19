from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mcpolis.domain.model.upstream import UpstreamDefinition


class UpstreamConfigStore(ABC):
    @abstractmethod
    async def get_all(self, org_id: str) -> list[UpstreamDefinition]: ...

    @abstractmethod
    async def get(self, org_id: str, upstream_id: str) -> UpstreamDefinition | None: ...

    @abstractmethod
    async def add(self, org_id: str, upstream: UpstreamDefinition) -> None: ...

    @abstractmethod
    async def update(self, org_id: str, upstream: UpstreamDefinition) -> None: ...

    @abstractmethod
    async def update_server_config(
        self, org_id: str, upstream_id: str, server_config: dict[str, Any]
    ) -> None: ...

    @abstractmethod
    async def remove(self, org_id: str, upstream_id: str) -> None: ...

    @abstractmethod
    async def delete_all_for_org(self, org_id: str) -> None: ...
