"""Every dashboard or operator request that changes something runs to its
end once started, whatever cancels it (``RunToCompletionMiddleware``).

The shared admin actions protect themselves (``runs_to_completion``);
the dashboard's other writes did not, and stopped wherever a cancel
caught them:

- an admin's MCP edit saved the new name and lost the Variables saved in
  the same click;
- an operator's sign-out-everywhere signed the person out and wrote no
  row on the customer's Audit page;
- a member's sign-out of an MCP deleted the saved sign-in and left the
  live session serving them.

Two kinds of cancel reach a request: an anyio cancel scope (Starlette's
``BaseHTTPMiddleware``) and a native ``Task.cancel()`` (uvicorn, for the
requests still running when its graceful-shutdown time is up). Each test
holds one request of each kind of route half-way, cancels it both ways,
lets it go on, and checks it reached its end. The routers are the real
ones, with the middleware the app installs.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from fastapi import APIRouter, FastAPI
from starlette.types import Message, Scope

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.email_allowlist import EmailAllowlist
from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.model.template_var import TemplateVarSummary
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.organization_repository import Membership, Organization
from mcpolis.domain.services.audit_actions import OPERATOR_SIGN_OUT_EVERYWHERE
from mcpolis.domain.services.role_admin_service import RoleAdminService
from mcpolis.domain.services.upstream_admin_service import UpstreamAdminService
from mcpolis.domain.services.user_admin_service import UserAdminService
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.middleware.run_to_completion import (
    RunToCompletionMiddleware,
    changes_state,
)
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard.auth_connect import (
    create_auth_connect_router,
)
from mcpolis.entrypoints.routes.dashboard.upstream_admin import (
    create_upstream_admin_router,
)
from mcpolis.entrypoints.routes.superadmin_routes import create_superadmin_router
from tests.unit._state_seed import seed_user_session
from tests.unit.factories import (
    Gate,
    GatedConnectionStore,
    cancel_natively_while_gated,
    cancel_while_gated,
)
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    AdminParts,
    _config_with_one_stdio,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
    make_oauth_token,
    make_oauth_upstream_config,
)
from tests.unit.test_mcp_endpoints_start_at_boot import make_standalone_settings
from tests.unit.test_superadmin_audit import InMemoryOrgRepo

CancelKind = Literal["anyio", "native"]
CANCEL_KINDS: list[CancelKind] = ["anyio", "native"]
MEMBER = "member@example.com"
OPERATOR = "op@mcphero.io"


async def cancel_while_held(
    kind: CancelKind,
    gate: Gate,
    call: Callable[[], Coroutine[object, object, object]],
) -> None:
    if kind == "anyio":
        await cancel_while_gated(gate, call)
    else:
        await cancel_natively_while_gated(gate, call)


def make_http_scope(method: str, path: str, body: bytes) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:8080"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8080),
    }


async def send_request(
    app: FastAPI, method: str, path: str, body: bytes = b"",
) -> None:
    """One request over plain ASGI, so the test can cancel the task that
    serves it. The client stays connected."""
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        del message

    await app(make_http_scope(method, path, body), receive, send)


def make_app(*routers: APIRouter) -> FastAPI:
    """The routers, with the middleware the real app puts outermost."""
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    app.add_middleware(RunToCompletionMiddleware)
    return app


def make_dashboard_deps(
    parts: AdminParts,
    *,
    user: str,
    template_var_repo: FileTemplateVarRepository,
) -> DashboardDeps:
    """The dashboard routers' deps over the Admin MCP harness's stores,
    signed in as ``user``."""
    deps = parts.action_deps

    def signed_in() -> str:
        return user

    return DashboardDeps(
        runtime_manager=deps.runtime_manager,
        policy_store=deps.policy_store,
        audit_repo=deps.audit_repo,
        connection_store=deps.connection_store,
        auth_coordinator=deps.auth_coordinator,
        server_url=deps.server_url,
        gateway_url=deps.server_url,
        get_current_user=signed_in,
        require_admin=signed_in,
        get_startup_status=None,
        get_gateway_connected_users=None,
        revoke_gateway_user=None,
        terminate_gateway_sessions=None,
        event_bus=None,
        list_admin_mcp_tools=None,
        allow_stdio_mcp=True,
        org_repo=deps.org_repo,
        is_cloud_mode=False,
        template_var_repo=template_var_repo,
        user_admin=UserAdminService(deps),
        upstream_admin=UpstreamAdminService(deps),
        role_admin=RoleAdminService(deps),
    )


class SetWaitsFirst(FileTemplateVarRepository):
    """Variables whose ``set`` waits at ``gate`` before it writes."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.gate = Gate()

    async def set(
        self,
        org_id: str,
        upstream_id: str,
        name: str,
        value: str,
        *,
        is_secret: bool = True,
    ) -> TemplateVarSummary:
        await self.gate.hold()
        return await super().set(
            org_id, upstream_id, name, value, is_secret=is_secret,
        )


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_an_admins_mcp_edit_saves_its_variables_too(
    tmp_path: Path, kind: CancelKind,
) -> None:
    """Edit an MCP's name and a Variable in one Save; the request is
    cancelled once the config is saved, before the Variable is."""
    cfg, mcps = _config_with_one_stdio()
    parts = await make_admin_parts(tmp_path, config=cfg, mcp_servers=mcps)
    variables = SetWaitsFirst(tmp_path / "data")
    app = make_app(create_upstream_admin_router(make_dashboard_deps(
        parts, user=ADMIN_EMAIL, template_var_repo=variables,
    )))
    body = json.dumps({
        "display_name": "Renamed",
        "template_var_changes": {
            "sets": {"API_KEY": {"value": "secret-123", "is_secret": True}},
        },
    }).encode()

    await cancel_while_held(
        kind, variables.gate,
        lambda: send_request(app, "PUT", "/api/admin/upstreams/s0", body),
    )

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    upstream = await runtime.config_service.get_upstream(DEFAULT_ORG_ID, "s0")
    assert upstream is not None and upstream.display_name == "Renamed"
    saved = await variables.list_summaries(DEFAULT_ORG_ID, "s0")
    assert [summary.name for summary in saved] == ["API_KEY"], (
        "the edit saved the new name and lost the Variable"
    )


def make_two_orgs_repo() -> InMemoryOrgRepo:
    orgs = [
        Organization(
            id=org_id, slug=org_id, display_name=org_id,
            created_at=datetime.now(UTC),
            subscription=Subscription(plan=PlanName.team),
        )
        for org_id in ("acme", "globex")
    ]
    memberships = [
        Membership(org_id=org.id, email=MEMBER, role="user") for org in orgs
    ]
    return InMemoryOrgRepo(orgs, memberships)


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_an_operators_sign_out_everywhere_is_audited_in_every_org(
    tmp_path: Path, kind: CancelKind,
) -> None:
    """The operator signs a person out of every org; the request is
    cancelled while their sessions close, before the rows that tell each
    org are written."""
    gate = Gate()
    closed_in: list[str] = []

    async def terminate(org_id: str, email: str) -> int:
        del email
        await gate.hold()
        closed_in.append(org_id)
        return 1

    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = create_superadmin_router(
        settings=Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path / "data",
            session_secret="test-session-secret",
        ),
        org_repo=make_two_orgs_repo(),  # type: ignore[arg-type]
        runtime_manager=None,  # type: ignore[arg-type]
        org_service=None,  # type: ignore[arg-type]
        audit_repo=audit,
        connection_store=FileConnectionStore(tmp_path),
        revoke_gateway_user=lambda _email: 1,
        terminate_gateway_sessions=terminate,
        get_current_user=lambda: OPERATOR,
        superadmin_emails=EmailAllowlist([OPERATOR]),
    )
    app = make_app(router)

    await cancel_while_held(
        kind, gate,
        lambda: send_request(
            app, "POST", f"/api/superadmin/users/{MEMBER}/sessions/revoke",
        ),
    )

    assert sorted(closed_in) == ["acme", "globex"]
    for org_id in ("acme", "globex"):
        rows = await audit.search_cross_org(org_id=org_id, limit=10)
        assert [r["action"] for r in rows] == [OPERATOR_SIGN_OUT_EVERYWHERE], (
            f"no row on {org_id}'s Audit page for the operator's sign-out"
        )


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_members_sign_out_also_closes_their_live_session(
    tmp_path: Path, kind: CancelKind,
) -> None:
    """A member signs out of an MCP; the request is cancelled once their
    saved sign-in is deleted, before their live session closes."""
    config, mcp_servers = make_oauth_upstream_config()
    store = GatedConnectionStore(tmp_path, "delete_user_token", after=True)
    await store.put_user_token(DEFAULT_ORG_ID, MEMBER, "notion", make_oauth_token())
    manager = UpstreamClientManager([])
    seed_user_session(manager, "notion", MEMBER)
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        client_manager=manager, connection_store=store,
        auth_coordinator=PendingAuthCoordinator(b"k" * 32),
    )
    app = make_app(create_auth_connect_router(make_dashboard_deps(
        parts, user=MEMBER,
        template_var_repo=FileTemplateVarRepository(tmp_path / "data"),
    )))

    await cancel_while_held(
        kind, store.gate,
        lambda: send_request(app, "POST", "/api/auth/disconnect/notion"),
    )

    assert await store.get_user_token(DEFAULT_ORG_ID, MEMBER, "notion") is None
    assert not manager.has_user_session("notion", MEMBER), (
        "the sign-in was deleted and the live session kept serving them"
    )


def test_the_app_runs_state_changing_requests_to_completion_outermost(
    tmp_path: Path,
) -> None:
    """Installed on the real app, outside every other middleware: inside a
    ``BaseHTTPMiddleware`` layer a cancelled request could never end."""
    app = create_app(make_standalone_settings(tmp_path))

    assert app.user_middleware[0].cls is RunToCompletionMiddleware


@pytest.mark.parametrize(("method", "path", "covered"), [
    ("POST", "/api/admin/upstreams", True),
    ("PUT", "/api/admin/upstreams/u1/template-vars/API_KEY", True),
    ("DELETE", "/api/admin/upstreams/u1/sandbox-files/f1", True),
    ("PATCH", "/api/superadmin/orgs/o1/subscription", True),
    ("POST", "/api/auth/disconnect/u1", True),
    # Reads, and streams that stay open until the client leaves.
    ("GET", "/api/admin/upstreams", False),
    ("GET", "/api/events", False),
    ("GET", "/api/admin/upstreams/u1/logs/stream", False),
    # The member sign-in waits up to 30 s for the browser's redirect URL.
    ("GET", "/api/auth/connect/u1", False),
    # MCP endpoints have their own protection.
    ("POST", "/mcp/", False),
    ("POST", "/admin-mcp/", False),
])
def test_which_requests_run_to_completion(
    method: str, path: str, covered: bool,
) -> None:
    assert changes_state(make_http_scope(method, path, b"")) == covered
