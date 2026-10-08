"""``record_action`` is the one writer for account-action audit rows."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog

from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.domain.services.audit_actions import MEMBER_REMOVED, record_action
from mcpolis.entrypoints.app import _SessionRegistrationMiddleware  # pyright: ignore[reportPrivateUsage]


class FailingAuditRepository(FileAuditRepository):
    """An audit store that is down."""

    async def log(self, org_id: str, entry: Any) -> None:
        del org_id, entry
        raise RuntimeError("audit store down")


@pytest.mark.asyncio
async def test_record_action_logs_and_continues_when_the_store_is_down(
    tmp_path: Path,
) -> None:
    """The action it records has already happened; a failed write must
    not turn that into an error for the person who acted."""
    repo = FailingAuditRepository(tmp_path / "audit.jsonl")

    with structlog.testing.capture_logs() as logs:
        await record_action(
            repo, "acme", action=MEMBER_REMOVED,
            actor="admin@acme.com", target_user_id="bob@acme.com",
        )

    failures = [e for e in logs if e["event"] == "audit.write_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "error"
    assert failures[0]["action"] == MEMBER_REMOVED
    assert failures[0]["org_id"] == "acme"


@pytest.mark.asyncio
async def test_client_connect_goes_through_when_the_audit_store_is_down(
    tmp_path: Path,
) -> None:
    """The ``client_connect`` row is written before the MCP request is
    handed on; a down audit store must not turn the request into an
    error, so the client can still connect."""
    handed_on: list[str] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        del receive, send
        handed_on.append(scope["path"])

    middleware = _SessionRegistrationMiddleware(
        downstream,
        session_registry=GatewaySessionRegistry(),
        audit_repo=FailingAuditRepository(tmp_path / "audit.jsonl"),
    )
    scope: dict[str, Any] = {
        "type": "http", "method": "POST", "path": "/mcp/",
        "headers": [(b"mcp-session-id", b"sid-1"), (b"user-agent", b"Claude Code")],
        "query_string": b"",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        del message

    with structlog.testing.capture_logs() as logs:
        await middleware(scope, receive, send)

    assert handed_on == ["/mcp/"]
    assert [e["action"] for e in logs if e["event"] == "audit.write_failed"] == [
        "client_connect",
    ]
