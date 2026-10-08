"""Operator (superadmin) actions leave audit rows in the org they touch.

``docs/operator-access.md`` promises the customer that an operator's
account actions are "recorded too, along with who did them". Before
this suite those actions wrote only a structured log line, which the
customer never sees. These tests pin the audit rows, and the cross-org
audit search's org filter.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.email_allowlist import EmailAllowlist
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.ports.organization_repository import Membership, Organization
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.superadmin_controller import (
    create_superadmin_mcp_server,
)
from mcpolis.entrypoints.routes.superadmin_routes import create_superadmin_router
from tests.unit.factories import make_audit_entry

OPERATOR = "op@mcphero.io"
TEAMMATE = "bob@acme.com"


class InMemoryOrgRepo:
    """Just enough ``OrganizationRepository`` for the operator routes."""

    def __init__(
        self, orgs: list[Organization], memberships: list[Membership],
    ) -> None:
        self._orgs = {o.id: o for o in orgs}
        self._memberships = memberships

    async def get_organization(self, org_id: str) -> Organization | None:
        return self._orgs.get(org_id)

    async def get_by_slug(self, slug: str) -> Organization | None:
        return next((o for o in self._orgs.values() if o.slug == slug), None)

    async def list_organizations(self) -> list[Organization]:
        return list(self._orgs.values())

    async def list_memberships(self, org_id: str) -> list[Membership]:
        return [m for m in self._memberships if m.org_id == org_id]

    async def get_memberships_for_email(self, email: str) -> list[Membership]:
        return [m for m in self._memberships if m.email == email]

    async def update_subscription(
        self, org_id: str, subscription: Subscription,
    ) -> None:
        org = self._orgs[org_id]
        self._orgs[org_id] = org.model_copy(update={"subscription": subscription})


def make_org(org_id: str, plan: PlanName = PlanName.free) -> Organization:
    return Organization(
        id=org_id, slug=org_id, display_name=org_id.title(),
        created_at=datetime.now(UTC), subscription=Subscription(plan=plan),
    )


def make_membership(org_id: str, email: str) -> Membership:
    return Membership(org_id=org_id, email=email, role="user")


def make_org_repo() -> InMemoryOrgRepo:
    """Two orgs; the teammate belongs to both, the operator to neither."""
    return InMemoryOrgRepo(
        [make_org("acme"), make_org("globex")],
        [make_membership("acme", TEAMMATE), make_membership("globex", TEAMMATE)],
    )


def make_superadmin_client(
    tmp_path: Path,
    org_repo: InMemoryOrgRepo,
    *,
    audit_repo: FileAuditRepository | None = None,
    terminated: list[str] | None = None,
) -> tuple[TestClient, FileAuditRepository, FileConnectionStore]:
    """The operator router as app.py mounts it, over in-memory orgs and
    file stores. ``terminated`` records each org whose gateway sessions
    were ended."""
    audit = audit_repo or FileAuditRepository(tmp_path / "audit.jsonl")
    connection_store = FileConnectionStore(tmp_path / "data")
    ended = terminated if terminated is not None else []

    async def terminate(org_id: str, _email: str) -> int:
        ended.append(org_id)
        return 1

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path / "data",
        session_secret="test-session-secret",
    )
    client_manager = UpstreamClientManager([])

    async def get_runtime(org_id: str) -> SimpleNamespace:
        del org_id
        return SimpleNamespace(client_manager=client_manager)

    router = create_superadmin_router(
        settings=settings,
        org_repo=org_repo,  # type: ignore[arg-type]
        runtime_manager=SimpleNamespace(get=get_runtime),  # type: ignore[arg-type]
        org_service=SimpleNamespace(),  # type: ignore[arg-type]
        audit_repo=audit,
        connection_store=connection_store,
        revoke_gateway_user=lambda _email: 2,
        terminate_gateway_sessions=terminate,
        get_current_user=lambda: OPERATOR,
        superadmin_emails=EmailAllowlist([OPERATOR]),
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), audit, connection_store


class FailingAuditRepository(FileAuditRepository):
    """An audit store that is down."""

    async def log(self, org_id: str, entry: Any) -> None:
        del org_id, entry
        raise RuntimeError("audit store down")


def make_token() -> OAuthToken:
    return OAuthToken(
        access_token="access-123", refresh_token="refresh-456",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC), scopes=[],
    )


async def org_rows(
    audit_repo: FileAuditRepository, org_id: str, action: str,
) -> list[dict[str, Any]]:
    return await audit_repo.search_cross_org(
        org_id=org_id, action=[action], limit=100,
    )


@pytest.mark.asyncio
async def test_operator_sign_out_everywhere_writes_a_row_in_each_org(
    tmp_path: Path,
) -> None:
    client, audit_repo, _ = make_superadmin_client(tmp_path, make_org_repo())

    resp = client.post(f"/api/superadmin/users/{TEAMMATE}/sessions/revoke")

    assert resp.status_code == 200
    for org_id in ("acme", "globex"):
        rows = await org_rows(audit_repo, org_id, "operator_sign_out_everywhere")
        assert len(rows) == 1, org_id
        row = rows[0]
        assert row["user_id"] == OPERATOR
        assert row["actor_role"] == "operator"
        assert row["target_user_id"] == TEAMMATE
        assert row["outcome"] == "success"


@pytest.mark.asyncio
async def test_operator_clear_sign_in_writes_a_row(tmp_path: Path) -> None:
    client, audit_repo, connection_store = make_superadmin_client(
        tmp_path, make_org_repo(),
    )
    await connection_store.put_user_token("acme", TEAMMATE, "github", make_token())

    resp = client.post(
        f"/api/superadmin/users/{TEAMMATE}/connections/acme/github/reauth",
    )

    assert resp.status_code == 200
    assert resp.json()["cleared"] is True
    assert await connection_store.get_user_token("acme", TEAMMATE, "github") is None
    rows = await org_rows(audit_repo, "acme", "operator_clear_sign_in")
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == OPERATOR
    assert row["actor_role"] == "operator"
    assert row["target_user_id"] == TEAMMATE
    assert row["upstream_id"] == "github"
    # Only the org whose sign-in was cleared gets the row.
    assert await org_rows(audit_repo, "globex", "operator_clear_sign_in") == []


@pytest.mark.asyncio
async def test_operator_plan_change_writes_a_row(tmp_path: Path) -> None:
    client, audit_repo, _ = make_superadmin_client(tmp_path, make_org_repo())

    resp = client.patch(
        "/api/superadmin/orgs/acme/subscription", json={"plan": "team"},
    )

    assert resp.status_code == 200
    rows = await org_rows(audit_repo, "acme", "operator_plan_change")
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == OPERATOR
    assert row["actor_role"] == "operator"
    assert row["detail"] == "free → team"


@pytest.mark.asyncio
async def test_operator_audit_search_filters_by_org_before_the_page_is_cut(
    tmp_path: Path,
) -> None:
    """Asking for 3 rows of one org returns 3 rows of that org, even when
    other orgs wrote newer rows. Filtering after the page is fetched
    returned fewer (here: none)."""
    client, audit_repo, _ = make_superadmin_client(tmp_path, make_org_repo())
    for i in range(3):
        await audit_repo.log("acme", make_audit_entry(
            org_id="acme", timestamp=f"2026-01-01T00:00:0{i}Z",
        ))
    for i in range(5):
        await audit_repo.log("globex", make_audit_entry(
            org_id="globex", timestamp=f"2026-01-02T00:00:0{i}Z",
        ))

    resp = client.get("/api/superadmin/audit?org_id=acme&limit=3")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 3
    assert {e["org_id"] for e in entries} == {"acme"}

    agg = client.get("/api/superadmin/audit/aggregates?org_id=acme&sample_size=100")
    assert agg.status_code == 200
    assert agg.json()["sample_size"] == 3


@pytest.mark.asyncio
async def test_operator_mcp_delete_org_logs_the_operator(tmp_path: Path) -> None:
    """No ``current_operator`` injected: the production default reads
    the operator from the OAuth bearer the SDK put on the request."""
    org_repo = make_org_repo()
    deleted: list[str] = []

    async def delete_organization(org_id: str) -> None:
        deleted.append(org_id)

    server = create_superadmin_mcp_server(
        org_repo=org_repo,  # type: ignore[arg-type]
        runtime_manager=SimpleNamespace(get_cached=lambda _id: None),  # type: ignore[arg-type]
        org_service=SimpleNamespace(delete_organization=delete_organization),  # type: ignore[arg-type]
    )

    auth_token = auth_context_var.set(AuthenticatedUser(AccessToken(
        token="fake", client_id=OPERATOR, scopes=[],
        expires_at=int(time.time()) + 3600,
    )))
    try:
        with structlog.testing.capture_logs() as logs:
            await server.call_tool(
                "delete_organization", {"slug": "acme", "confirm": True},
            )
    finally:
        auth_context_var.reset(auth_token)

    assert deleted == ["acme"]
    events = [e for e in logs if e["event"] == "superadmin.organization.deleted"]
    assert len(events) == 1
    assert events[0]["actor"] == OPERATOR


@pytest.mark.asyncio
async def test_operator_sign_out_reaches_every_org_when_the_audit_store_is_down(
    tmp_path: Path,
) -> None:
    """A failed audit write must not stop the sign-out halfway: every
    org's sessions end, and the operator gets a normal answer."""
    org_repo = InMemoryOrgRepo(
        [make_org("acme"), make_org("globex"), make_org("initech")],
        [make_membership(o, TEAMMATE) for o in ("acme", "globex", "initech")],
    )
    terminated: list[str] = []
    client, _, _ = make_superadmin_client(
        tmp_path, org_repo,
        audit_repo=FailingAuditRepository(tmp_path / "audit.jsonl"),
        terminated=terminated,
    )

    resp = client.post(f"/api/superadmin/users/{TEAMMATE}/sessions/revoke")

    assert resp.status_code == 200
    assert sorted(terminated) == ["acme", "globex", "initech"]


@pytest.mark.asyncio
async def test_operator_plan_change_to_the_same_plan_writes_no_row(
    tmp_path: Path,
) -> None:
    client, audit_repo, _ = make_superadmin_client(tmp_path, make_org_repo())

    resp = client.patch(
        "/api/superadmin/orgs/acme/subscription", json={"plan": "free"},
    )

    assert resp.status_code == 200
    assert await org_rows(audit_repo, "acme", "operator_plan_change") == []


@pytest.mark.asyncio
async def test_operator_clear_sign_in_with_nothing_to_clear_writes_no_row(
    tmp_path: Path,
) -> None:
    """A typo in the email or the MCP must not put "Cleared x's sign-in"
    on the customer's Audit page."""
    client, audit_repo, _ = make_superadmin_client(tmp_path, make_org_repo())

    resp = client.post(
        f"/api/superadmin/users/{TEAMMATE}/connections/acme/githbu/reauth",
    )

    assert resp.status_code == 200
    assert resp.json()["cleared"] is False
    assert await org_rows(audit_repo, "acme", "operator_clear_sign_in") == []


@pytest.mark.asyncio
async def test_operator_audit_aggregates_count_denied_calls(
    tmp_path: Path,
) -> None:
    """Denied tool calls are written as ``denied``; the top deny rules
    must count them."""
    client, audit_repo, _ = make_superadmin_client(tmp_path, make_org_repo())
    await audit_repo.log("acme", make_audit_entry(
        org_id="acme", policy_decision="denied", policy_rule="mcp_disabled",
    ))

    resp = client.get("/api/superadmin/audit/aggregates?org_id=acme&sample_size=100")

    assert resp.status_code == 200
    assert resp.json()["top_deny_rules"] == [{"key": "mcp_disabled", "count": 1}]
