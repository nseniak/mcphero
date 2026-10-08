"""Dashboard role rename and delete, and token minting, driven through
the routers with ``DashboardDeps`` built by hand, so the event channel
and the token registry can be injected.

Pins what the full ``create_app`` tests cannot see:

- the policy-change notice goes out under the NEW role name;
- the running policy is reloaded right after the stored rename, before
  the token and membership writes, so a failure in those writes never
  leaves the running policy behind the stored one. Renaming the role
  back then repairs the half-applied rename;
- a rename, a delete and a mint on the same org never overlap. An
  overlap can leave a token on a role that no longer exists, which
  gets zero tools;
- adding a user whose role is renamed a moment earlier is refused, not
  saved on a role that no longer exists;
- a cancelled request never stops a rename or a delete half-way.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.admin_actions import (
    AdminActionDeps,
    AdminActionRefused,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.role_admin_service import RoleAdminService
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.domain.services.upstream_admin_service import UpstreamAdminService
from mcpolis.domain.services.user_admin_service import UserAdminService
from mcpolis.entrypoints.controllers.admin_action_errors import (
    refusal_detail,
    refusal_status,
)
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard.roles import create_roles_router
from mcpolis.entrypoints.routes.dashboard.service_tokens_admin import (
    create_service_tokens_admin_router,
)
from mcpolis.entrypoints.routes.dashboard.users_admin import (
    create_users_admin_router,
)
from tests.unit.factories import (
    GatedConfigStore,
    GatedTokenRepository,
    RecordingEventBus,
    RenameFailsOnceTokenRepository,
    cancel_natively_while_gated,
    cancel_while_gated,
    make_runtime_manager,
    run_while_gated,
)

ADMIN_EMAIL = "admin@example.com"


def make_config_file(tmp_path: Path) -> Path:
    """``spare`` has a user, so the store itself refuses to delete it.
    ``bots`` has none, so only the token check stands in a delete's way."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "upstreams": {},
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
            "spare": {"settings": {"mcp_access": {"mcps": {}}}},
            "bots": {"settings": {"mcp_access": {"mcps": {}}}},
        },
        "users": {
            ADMIN_EMAIL: {"role": "admin"},
            "member@example.com": {"role": "spare"},
        },
    }))
    return path


def make_dashboard_app(
    tmp_path: Path,
    *,
    token_repo: FileServiceTokenRepository,
    event_bus: RecordingEventBus | None = None,
    config_store: FileConfigStore | None = None,
) -> FastAPI:
    """*config_store*, when given, must be built on ``make_config_file``.
    Wired like ``create_dashboard_api_router`` and ``create_app``: the
    shared admin actions, and the handler that turns their refusals into
    HTTP answers."""
    config_store = config_store or FileConfigStore(make_config_file(tmp_path))
    policy_engine = PolicyEngine(config_store.ensure_defaults_sync(DEFAULT_ORG_ID))
    runtime_manager = make_runtime_manager(policy_engine)
    audit_repo = FileAuditRepository(tmp_path / "audit.jsonl")
    org_repo = FileOrganizationRepository(tmp_path / "data")
    template_var_repo = FileTemplateVarRepository(tmp_path / "data")
    event_bus = event_bus or RecordingEventBus()

    def require_admin() -> str:
        return ADMIN_EMAIL

    token_service = ServiceTokenService(repo=token_repo)
    action_deps = AdminActionDeps(
        runtime_manager=runtime_manager,
        policy_store=config_store,
        audit_repo=audit_repo,
        connection_store=None,
        auth_coordinator=None,
        server_url="http://localhost:8000",
        event_bus=event_bus,
        org_repo=org_repo,
        allow_stdio_mcp=True,
        template_var_repo=template_var_repo,
        service_token_service=token_service,
    )
    deps = DashboardDeps(
        runtime_manager=runtime_manager,
        policy_store=config_store,
        audit_repo=audit_repo,
        connection_store=None,
        auth_coordinator=None,
        server_url="http://localhost:8000",
        gateway_url="http://localhost:8000",
        get_current_user=require_admin,
        require_admin=require_admin,
        get_startup_status=None,
        get_gateway_connected_users=None,
        revoke_gateway_user=None,
        terminate_gateway_sessions=None,
        event_bus=event_bus,
        list_admin_mcp_tools=None,
        allow_stdio_mcp=True,
        org_repo=org_repo,
        is_cloud_mode=False,
        template_var_repo=template_var_repo,
        user_admin=UserAdminService(action_deps),
        upstream_admin=UpstreamAdminService(action_deps),
        role_admin=RoleAdminService(action_deps),
        service_token_service=token_service,
    )
    app = FastAPI()
    app.include_router(create_roles_router(deps))
    app.include_router(create_service_tokens_admin_router(deps))
    app.include_router(create_users_admin_router(deps))

    @app.exception_handler(AdminActionRefused)
    async def admin_action_refused(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: AdminActionRefused,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=refusal_status(exc),
            content={"detail": refusal_detail(exc)},
        )

    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    """In-process client on the test's event loop, so two requests can
    run at the same time. A server error comes back as a 500."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def mint_token(repo: FileServiceTokenRepository, role_name: str) -> None:
    await ServiceTokenService(repo=repo).mint(
        org_id=DEFAULT_ORG_ID, label="ci-bot", role_name=role_name,
        created_by=ADMIN_EMAIL,
    )


async def seed_membership(tmp_path: Path, email: str, role: str) -> None:
    """Write a membership row to disk before the app's repo loads it."""
    await FileOrganizationRepository(tmp_path / "data").add_membership(
        DEFAULT_ORG_ID, email, role,
    )


async def read_token_roles(repo: FileServiceTokenRepository) -> dict[str, str]:
    return {r.label: r.role_name for r in await repo.list_for_org(DEFAULT_ORG_ID)}


async def read_membership_roles(tmp_path: Path) -> dict[str, str]:
    rows = await FileOrganizationRepository(tmp_path / "data").list_memberships(
        DEFAULT_ORG_ID,
    )
    return {m.email: m.role for m in rows}


async def listed_role_names(client: httpx.AsyncClient) -> set[str]:
    resp = await client.get("/api/admin/roles")
    assert resp.status_code == 200, resp.text
    return {r["name"] for r in resp.json()}


async def rename(
    client: httpx.AsyncClient, old: str, new: str,
) -> httpx.Response:
    return await client.put(
        f"/api/admin/roles/{old}/rename", json={"new_name": new},
    )


# --- rename: notice, carry-along, failure ---


@pytest.mark.asyncio
async def test_rename_notifies_under_the_new_name(tmp_path: Path) -> None:
    """The old name matches nobody after the rename, so a notice under
    it would reach no session."""
    bus = RecordingEventBus()
    token_repo = FileServiceTokenRepository(tmp_path / "data")
    await mint_token(token_repo, "spare")
    await seed_membership(tmp_path, "member@example.com", "spare")
    app = make_dashboard_app(tmp_path, token_repo=token_repo, event_bus=bus)

    async with make_client(app) as client:
        resp = await rename(client, "spare", "renamed")

    assert resp.status_code == 200, resp.text
    notices = [e.payload for e in bus.events if e.type == "policy_changed"]
    assert notices == [{"role": "renamed"}]
    assert await read_token_roles(token_repo) == {"ci-bot": "renamed"}
    assert await read_membership_roles(tmp_path) == {
        "member@example.com": "renamed",
    }


@pytest.mark.asyncio
async def test_failed_token_write_leaves_running_policy_on_the_stored_rename(
    tmp_path: Path,
) -> None:
    token_repo = RenameFailsOnceTokenRepository(tmp_path / "data")
    await mint_token(token_repo, "spare")
    app = make_dashboard_app(tmp_path, token_repo=token_repo)

    async with make_client(app) as client:
        resp = await rename(client, "spare", "renamed")
        # The admin sees a failure, not a false success...
        assert resp.status_code == 500
        # ...but the running policy already matches the stored rename.
        names = await listed_role_names(client)
        assert "renamed" in names
        assert "spare" not in names
        # The token write failed, so the token is still on the old name.
        assert await read_token_roles(token_repo) == {"ci-bot": "spare"}

        # Repair: renaming the role back makes everything agree again.
        resp = await rename(client, "renamed", "spare")
        assert resp.status_code == 200, resp.text
        assert "spare" in await listed_role_names(client)
        assert await read_token_roles(token_repo) == {"ci-bot": "spare"}
        # And a second try of the rename now goes through.
        resp = await rename(client, "spare", "renamed")
        assert resp.status_code == 200, resp.text
    assert await read_token_roles(token_repo) == {"ci-bot": "renamed"}


# --- rename, delete and mint never overlap ---


@pytest.mark.asyncio
async def test_rename_waits_for_a_token_being_created(tmp_path: Path) -> None:
    """A rename that lands between a mint's role check and its insert
    would leave the new token on the old name."""
    token_repo = GatedTokenRepository(tmp_path / "data", gated="create")
    app = make_dashboard_app(tmp_path, token_repo=token_repo)

    async with make_client(app) as client:
        minted, renamed, overlapped = await run_while_gated(
            token_repo.gate,
            lambda: client.post(
                "/api/admin/service-tokens",
                json={"label": "ci-bot", "role": "bots"},
            ),
            lambda: rename(client, "bots", "robots"),
        )

    assert not overlapped
    assert minted.status_code == 201, minted.text
    assert renamed.status_code == 200, renamed.text
    assert await read_token_roles(token_repo) == {"ci-bot": "robots"}


@pytest.mark.asyncio
async def test_delete_waits_for_a_rename_in_progress(tmp_path: Path) -> None:
    """A delete that runs its token check before a rename has moved the
    tokens counts zero, and deletes the role the tokens are moving to."""
    token_repo = GatedTokenRepository(tmp_path / "data", gated="rename_role")
    await mint_token(token_repo, "bots")
    app = make_dashboard_app(tmp_path, token_repo=token_repo)

    async with make_client(app) as client:
        renamed, deleted, overlapped = await run_while_gated(
            token_repo.gate,
            lambda: rename(client, "bots", "robots"),
            lambda: client.delete("/api/admin/roles/robots"),
        )
        names = await listed_role_names(client)

    assert not overlapped
    assert renamed.status_code == 200, renamed.text
    assert deleted.status_code == 400
    assert "service token" in deleted.json()["detail"]
    assert "robots" in names
    assert await read_token_roles(token_repo) == {"ci-bot": "robots"}


@pytest.mark.asyncio
async def test_delete_waits_for_a_token_being_created(tmp_path: Path) -> None:
    """A delete that runs its token check before a mint's insert counts
    zero, and deletes the role the new token is about to hold."""
    token_repo = GatedTokenRepository(tmp_path / "data", gated="create")
    app = make_dashboard_app(tmp_path, token_repo=token_repo)

    async with make_client(app) as client:
        minted, deleted, overlapped = await run_while_gated(
            token_repo.gate,
            lambda: client.post(
                "/api/admin/service-tokens",
                json={"label": "ci-bot", "role": "bots"},
            ),
            lambda: client.delete("/api/admin/roles/bots"),
        )
        names = await listed_role_names(client)

    assert not overlapped
    assert minted.status_code == 201, minted.text
    assert deleted.status_code == 400
    assert "service token" in deleted.json()["detail"]
    assert "bots" in names
    assert await read_token_roles(token_repo) == {"ci-bot": "bots"}


@pytest.mark.asyncio
async def test_mint_waits_for_a_rename_in_progress(tmp_path: Path) -> None:
    """A mint that checks the role before a rename's reload still sees
    the old name, passes, and then inserts a token on a role that is
    gone. Inside the lock, it checks after the rename and is refused."""
    config_store = GatedConfigStore(make_config_file(tmp_path), gated="rename_role")
    token_repo = FileServiceTokenRepository(tmp_path / "data")
    app = make_dashboard_app(
        tmp_path, token_repo=token_repo, config_store=config_store,
    )

    async with make_client(app) as client:
        renamed, minted, overlapped = await run_while_gated(
            config_store.gate,
            lambda: rename(client, "bots", "robots"),
            lambda: client.post(
                "/api/admin/service-tokens",
                json={"label": "ci-bot", "role": "bots"},
            ),
        )

    assert not overlapped
    assert renamed.status_code == 200, renamed.text
    assert minted.status_code == 400
    assert "not found" in minted.json()["detail"]
    assert await read_token_roles(token_repo) == {}


@pytest.mark.asyncio
async def test_add_user_is_refused_when_its_role_is_renamed_mid_way(
    tmp_path: Path,
) -> None:
    """Adding a user checks the role, then saves. A rename in between
    must not leave the new user on a role that no longer exists."""
    config_store = GatedConfigStore(make_config_file(tmp_path), gated="set_user")
    app = make_dashboard_app(
        tmp_path,
        token_repo=FileServiceTokenRepository(tmp_path / "data"),
        config_store=config_store,
    )

    async with make_client(app) as client:
        added, renamed, overlapped = await run_while_gated(
            config_store.gate,
            lambda: client.post(
                "/api/admin/users",
                json={"email": "new@example.com", "role": "bots"},
            ),
            lambda: rename(client, "bots", "robots"),
        )

    # The rename really landed between the check and the save.
    assert overlapped
    assert renamed.status_code == 200, renamed.text
    assert added.status_code == 400
    assert "Role 'bots' not found" in added.json()["detail"]
    users = (await config_store.load(DEFAULT_ORG_ID)).users
    assert "new@example.com" not in users


# --- a cancelled request never stops a role change half-way ---
# Starlette does not cancel a request when the client goes away, but
# other layers can. A ``BaseHTTPMiddleware`` layer cancels through an
# anyio scope (``cancel_while_gated``); uvicorn, when its graceful-shutdown
# time is up, cancels the request task natively, which an anyio shield
# does not stop (``cancel_natively_while_gated``). Neither may cut a
# rename or a delete, whatever middleware sits above the route.


@pytest.mark.asyncio
async def test_cancelled_rename_still_moves_the_tokens(tmp_path: Path) -> None:
    """Cancelled after the role was renamed but before the tokens moved,
    the tokens would keep the old name, with zero tools."""
    token_repo = GatedTokenRepository(tmp_path / "data", gated="rename_role")
    await mint_token(token_repo, "bots")
    app = make_dashboard_app(tmp_path, token_repo=token_repo)

    async with make_client(app) as client:
        await cancel_while_gated(
            token_repo.gate, lambda: rename(client, "bots", "robots"),
        )
        names = await listed_role_names(client)

    assert "robots" in names
    assert "bots" not in names
    assert await read_token_roles(token_repo) == {"ci-bot": "robots"}


@pytest.mark.asyncio
async def test_cancelled_delete_still_reloads_the_policy(tmp_path: Path) -> None:
    """Cancelled after the stored delete but before the reload, the
    running policy would keep the deleted role, so a token could still
    be minted on it."""
    config_store = GatedConfigStore(
        make_config_file(tmp_path), gated="delete_role", after=True,
    )
    app = make_dashboard_app(
        tmp_path,
        token_repo=FileServiceTokenRepository(tmp_path / "data"),
        config_store=config_store,
    )

    async with make_client(app) as client:
        await cancel_while_gated(
            config_store.gate, lambda: client.delete("/api/admin/roles/bots"),
        )
        names = await listed_role_names(client)

    assert "bots" not in names


@pytest.mark.asyncio
async def test_a_native_cancel_does_not_cut_a_rename(tmp_path: Path) -> None:
    """With no ``BaseHTTPMiddleware`` layer above the route to turn it
    into an anyio cancel, a native cancel cut the rename between the
    role and its tokens: the tokens kept the old name, with zero tools."""
    token_repo = GatedTokenRepository(tmp_path / "data", gated="rename_role")
    await mint_token(token_repo, "bots")
    app = make_dashboard_app(tmp_path, token_repo=token_repo)

    async with make_client(app) as client:
        await cancel_natively_while_gated(
            token_repo.gate, lambda: rename(client, "bots", "robots"),
        )
        names = await listed_role_names(client)

    assert "robots" in names
    assert "bots" not in names
    assert await read_token_roles(token_repo) == {"ci-bot": "robots"}


@pytest.mark.asyncio
async def test_a_native_cancel_does_not_cut_a_delete(tmp_path: Path) -> None:
    config_store = GatedConfigStore(
        make_config_file(tmp_path), gated="delete_role", after=True,
    )
    app = make_dashboard_app(
        tmp_path,
        token_repo=FileServiceTokenRepository(tmp_path / "data"),
        config_store=config_store,
    )

    async with make_client(app) as client:
        await cancel_natively_while_gated(
            config_store.gate, lambda: client.delete("/api/admin/roles/bots"),
        )
        names = await listed_role_names(client)

    assert "bots" not in names
