from __future__ import annotations

from typing import Any

from mcpolis.domain.model.audit import AuditEntry


class AuditRepository:
    """Abstract base for audit log persistence."""

    async def log(self, org_id: str, entry: AuditEntry) -> None:
        raise NotImplementedError

    async def search(
        self,
        org_id: str,
        user_id: str | None = None,
        mcp_id: str | None = None,
        tool: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        since_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def search_cross_org(
        self,
        user_id: str | None = None,
        mcp_id: str | None = None,
        tool: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_filter_values(self, org_id: str) -> dict[str, list[str]]:
        raise NotImplementedError
