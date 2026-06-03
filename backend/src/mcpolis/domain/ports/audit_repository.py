from __future__ import annotations

from typing import Any, Protocol

from mcpolis.domain.model.audit import AuditEntry


class AuditRepository(Protocol):
    async def log(self, org_id: str, entry: AuditEntry) -> None: ...

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
    ) -> list[dict[str, Any]]: ...

    async def search_cross_org(
        self,
        user_id: str | None = None,
        mcp_id: str | None = None,
        tool: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search the audit log across every org. Operator-only.

        Implementations bypass the per-org scoping that ``search``
        enforces. Callers MUST gate this behind a superadmin check —
        unscoped access to audit data is an operator privilege.

        Each returned entry carries an ``org_id`` field so the caller
        can route or display it.
        """
        ...

    async def get_filter_values(self, org_id: str) -> dict[str, list[str]]: ...
