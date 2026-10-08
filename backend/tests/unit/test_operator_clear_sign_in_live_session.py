"""The operator's "clear sign-in" ends the live upstream session too.

``POST /api/superadmin/users/{email}/connections/{org}/{upstream}/reauth``
promises that the user's next request asks them to sign in again. It
used to delete the saved sign-in only, while a live per-user session
kept serving the user from its in-memory tokens. It now signs the user
out the one way every other door does (``sign_out_of_upstream``): the
sign-in goes, then the live session closes.

Real streamable-HTTP MCP server on loopback, real file store, real
client manager (``_user_session_harness``).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.email_allowlist import EmailAllowlist
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.organization_repository import Membership, Organization
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.routes.superadmin_routes import create_superadmin_router
from tests.unit._user_session_harness import (
    ALICE,
    UPSTREAM_ID,
    ConnectionGate,
    acquire,
    make_store,
    make_upstream,
    start_upstream,
    stop_upstream,
)
from tests.unit.test_superadmin_audit import InMemoryOrgRepo

OPERATOR = "op@mcphero.io"


def make_org_repo() -> InMemoryOrgRepo:
    org = Organization(
        id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_ID, display_name="Default",
        created_at=datetime.now(UTC), subscription=Subscription(plan=PlanName.free),
    )
    return InMemoryOrgRepo(
        [org], [Membership(org_id=DEFAULT_ORG_ID, email=ALICE, role="user")],
    )


def make_operator_app(
    tmp_path: Path, store: FileConnectionStore, mgr: UpstreamClientManager,
) -> FastAPI:
    async def get_runtime(org_id: str) -> SimpleNamespace:
        del org_id
        return SimpleNamespace(client_manager=mgr)

    async def terminate(org_id: str, email: str) -> int:
        del org_id, email
        return 0

    router = create_superadmin_router(
        settings=Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path / "data",
            session_secret="test-session-secret",
        ),
        org_repo=make_org_repo(),  # type: ignore[arg-type]
        runtime_manager=SimpleNamespace(get=get_runtime),  # type: ignore[arg-type]
        org_service=SimpleNamespace(),  # type: ignore[arg-type]
        audit_repo=FileAuditRepository(tmp_path / "audit.jsonl"),
        connection_store=store,
        revoke_gateway_user=lambda _email: 0,
        terminate_gateway_sessions=terminate,
        get_current_user=lambda: OPERATOR,
        superadmin_emails=EmailAllowlist([OPERATOR]),
    )
    app = FastAPI()
    app.include_router(router)
    return app


async def test_operator_clear_sign_in_ends_the_live_session(tmp_path: Path) -> None:
    server, task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    mgr = UpstreamClientManager([upstream])
    try:
        store = await make_store(tmp_path, {ALICE: "old-bearer"})
        live = await acquire(mgr, upstream, store)
        assert mgr.has_user_session(UPSTREAM_ID, ALICE)

        async with AsyncClient(
            transport=ASGITransport(app=make_operator_app(tmp_path, store, mgr)),
            base_url="http://t",
        ) as http:
            resp = await http.post(
                f"/api/superadmin/users/{ALICE}/connections/"
                f"{DEFAULT_ORG_ID}/{UPSTREAM_ID}/reauth",
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["cleared"] is True
        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is None
        assert not mgr.has_user_session(UPSTREAM_ID, ALICE)
        # The next request finds no session and no sign-in to rebuild one
        # from: it must sign in again.
        try:
            session_after = await acquire(mgr, upstream, store)
        except Exception:
            session_after = None
        assert session_after is None or session_after is not live
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)
