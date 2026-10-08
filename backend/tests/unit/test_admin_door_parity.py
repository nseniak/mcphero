"""The two admin doors, the dashboard and the Admin MCP, do the same
thing for the same action.

- Both audit the same admin actions, with the same row: add and remove an
  MCP, invite a teammate, change their role, create, rename and delete a
  role, mint and revoke a service token (the dashboard only), and Refresh
  tools. The Admin MCP used to audit none of these, the dashboard only the
  refresh.
- Refresh tools is one shared action: it refuses an MCP that is not
  running, and never signs the calling admin in. The Admin MCP's own copy
  signed the caller in when nobody had a live session, past the rule that
  one admin at a time holds an MCP's sign-in.
- ``get_upstream`` says whether an OAuth client secret or a
  service-account token is set, never its value, and hides any other
  credential of the connection settings: an AI client reads it.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
    UserReconnect,
)
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit.factories import make_stored_oauth_token
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    DEPUTY_EMAIL,
    AdminParts,
    _call,  # pyright: ignore[reportPrivateUsage]
    _make_service_token_service,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
    make_config_two_admins,
    make_oauth_upstream_config,
)
from tests.unit.test_dashboard_api import make_test_client

# What the Audit page needs from a row of an admin action: (action, who
# acted, the MCP, the teammate, the role or token).
AuditFacts = tuple[str, str, str, str | None, str | None]


def audit_facts(rows: list[dict[str, Any]], actions: set[str]) -> list[AuditFacts]:
    """The rows of ``actions``, oldest first."""
    return [
        (
            str(row["action"]),
            str(row["user_id"]),
            str(row["upstream_id"]),
            row.get("target_user_id"),
            row.get("detail"),
        )
        for row in reversed(rows)
        if row["action"] in actions
    ]


ADMIN_ACTIONS = {
    "upstream_added", "upstream_removed", "member_invited",
    "member_role_changed", "role_created", "role_renamed", "role_deleted",
    "service_token_created", "service_token_revoked",
}


async def seed_members(tmp_path: Path, rows: dict[str, str]) -> None:
    """Accepted invitations: membership rows, email → role."""
    repo = FileOrganizationRepository(tmp_path / "data")
    for email, role in rows.items():
        await repo.add_membership(DEFAULT_ORG_ID, email, role)


# --- audit rows ---


def test_the_dashboard_audits_its_admin_actions(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)

    answers = [
        client.post("/api/admin/upstreams", json={
            "id": "late", "display_name": "Late",
            "url": "http://127.0.0.1:9999/mcp",
        }),
        client.delete("/api/admin/upstreams/late"),
        client.post(
            "/api/admin/users",
            json={"email": "new@example.com", "role": "developer"},
        ),
        client.put(
            "/api/admin/users/dev@example.com/role", json={"role": "admin"},
        ),
        client.post("/api/admin/roles", json={"name": "reader"}),
        client.put(
            "/api/admin/roles/reader/rename", json={"new_name": "auditor"},
        ),
        client.post(
            "/api/admin/service-tokens",
            json={"label": "ci-bot", "role": "auditor"},
        ),
        client.delete("/api/admin/service-tokens/ci-bot"),
        client.delete("/api/admin/roles/auditor"),
    ]

    assert [a.status_code for a in answers] == [
        201, 200, 201, 200, 201, 200, 201, 200, 200,
    ], [a.text for a in answers]
    rows = asyncio.run(FileAuditRepository(
        tmp_path / "data" / "audit.jsonl",
    ).search(DEFAULT_ORG_ID, limit=100))
    admin = "admin@example.com"
    assert audit_facts(rows, ADMIN_ACTIONS) == [
        ("upstream_added", admin, "late", None, None),
        ("upstream_removed", admin, "late", None, None),
        ("member_invited", admin, "", "new@example.com", "developer"),
        ("member_role_changed", admin, "", "dev@example.com", "developer → admin"),
        ("role_created", admin, "", None, "reader"),
        ("role_renamed", admin, "", None, "reader → auditor"),
        ("service_token_created", admin, "", None, "ci-bot (role auditor)"),
        ("service_token_revoked", admin, "", None, "ci-bot"),
        ("role_deleted", admin, "", None, "auditor"),
    ]


async def test_the_admin_mcp_audits_its_admin_actions(tmp_path: Path) -> None:
    await seed_members(tmp_path, {ADMIN_EMAIL: "admin", DEPUTY_EMAIL: "admin"})
    parts = await make_admin_parts(
        tmp_path,
        config=make_config_two_admins(),
        plan=PlanName.team,
        service_token_service=_make_service_token_service(tmp_path),
    )

    answers = [
        await _call(parts.server, "add_upstream", {
            "mcp_id": "late", "display_name": "Late",
            "transport": "stdio", "command": "echo",
        }),
        await _call(parts.server, "remove_upstream", {"mcp_id": "late"}),
        await _call(
            parts.server, "add_user",
            {"email": "new@example.com", "role": "user"},
        ),
        await _call(
            parts.server, "set_user_role",
            {"email": DEPUTY_EMAIL, "role": "user"},
        ),
        await _call(parts.server, "create_role", {"name": "reader"}),
        await _call(
            parts.server, "rename_role",
            {"role_name": "reader", "new_name": "auditor"},
        ),
        await _call(parts.server, "delete_role", {"role_name": "auditor"}),
    ]

    assert not [a for a in answers if a.startswith("Error")], answers
    rows = await parts.audit_repo.search(DEFAULT_ORG_ID, limit=100)
    assert audit_facts(rows, ADMIN_ACTIONS) == [
        ("upstream_added", ADMIN_EMAIL, "late", None, None),
        ("upstream_removed", ADMIN_EMAIL, "late", None, None),
        ("member_invited", ADMIN_EMAIL, "", "new@example.com", "user"),
        ("member_role_changed", ADMIN_EMAIL, "", DEPUTY_EMAIL, "admin → user"),
        ("role_created", ADMIN_EMAIL, "", None, "reader"),
        ("role_renamed", ADMIN_EMAIL, "", None, "reader → auditor"),
        ("role_deleted", ADMIN_EMAIL, "", None, "auditor"),
    ]


# --- Refresh tools ---


class LiveUpstreamClientManager(UpstreamClientManager):
    """Every upstream's shared session is live: ``session``."""

    def __init__(self, session: ClientSession) -> None:
        super().__init__([])
        self._session = session

    def is_connected(self, upstream_id: str) -> bool:
        del upstream_id
        return True

    async def ensure_shared_connected(
        self, upstream: UpstreamDefinition,
    ) -> ClientSession:
        del upstream
        return self._session


@asynccontextmanager
async def make_upstream_session() -> AsyncIterator[ClientSession]:
    """A real MCP session to an upstream with two tools."""
    upstream = FastMCP(name="upstream")

    @upstream.tool(name="create_issue")
    async def create_issue() -> str:  # pyright: ignore[reportUnusedFunction]
        return "created"

    @upstream.tool(name="list_issues")
    async def list_issues() -> str:  # pyright: ignore[reportUnusedFunction]
        return "[]"

    async with create_connected_server_and_client_session(upstream) as session:
        yield session


def make_refreshable_config() -> tuple[dict[str, Any], dict[str, Any]]:
    config = make_config_two_admins()
    config["upstreams"] = {
        "github": {"display_name": "GitHub", "auth_mode": "service_account"},
    }
    return config, {"github": {"url": "http://127.0.0.1:9000/mcp"}}


async def wait_for_rows(parts: AdminParts, action: str) -> list[dict[str, Any]]:
    """The rows of ``action`` once there is one: the refresh audits in the
    background, after the dashboard's "Fetching info" pill has shown."""
    async with asyncio.timeout(10):
        while True:
            rows = await parts.audit_repo.search(DEFAULT_ORG_ID, limit=50)
            found = [row for row in rows if row["action"] == action]
            if found:
                return found
            await asyncio.sleep(0.05)


async def test_the_admin_mcp_refresh_reports_the_tool_count_and_is_audited(
    tmp_path: Path,
) -> None:
    config, mcp_servers = make_refreshable_config()
    async with make_upstream_session() as session:
        parts = await make_admin_parts(
            tmp_path, config=config, mcp_servers=mcp_servers,
            plan=PlanName.team,
            client_manager=LiveUpstreamClientManager(session),
        )

        text = await _call(
            parts.server, "refresh_upstream_tools", {"mcp_id": "github"},
        )
        rows = await wait_for_rows(parts, "refresh_tools")

    assert text == "Refreshed 2 tools from upstream MCP 'github'."
    assert [
        (row["user_id"], row["upstream_id"], row["outcome"]) for row in rows
    ] == [(ADMIN_EMAIL, "github", "success")]


async def test_the_admin_mcp_refresh_refuses_an_oauth_mcp_nobody_signed_in_to(
    tmp_path: Path,
) -> None:
    """The old copy signed the calling admin in to refresh."""
    config, mcp_servers = make_oauth_upstream_config()
    coordinator = PendingAuthCoordinator(b"k" * 32)
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        plan=PlanName.team, auth_coordinator=coordinator,
    )

    text = await _call(
        parts.server, "refresh_upstream_tools", {"mcp_id": "notion"},
    )

    assert "is not running" in text, text
    assert coordinator.get_pending(DEFAULT_ORG_ID, "notion", ADMIN_EMAIL) is None
    store = parts.action_deps.connection_store
    assert store is not None
    assert await store.get_user_token(DEFAULT_ORG_ID, ADMIN_EMAIL, "notion") is None
    rows = await parts.audit_repo.search(DEFAULT_ORG_ID, limit=50)
    assert [row for row in rows if row["action"] == "refresh_tools"] == []


class LiveUserSessionsClientManager(UpstreamClientManager):
    """Every user's own session is live: ``session``. Records whose
    session each caller asked for."""

    def __init__(self, session: ClientSession) -> None:
        super().__init__([])
        self._session = session
        self.sessions_of: list[str] = []

    async def ensure_user_session(
        self,
        upstream: UpstreamDefinition,
        user_id: str,
        *,
        auth: httpx.Auth | None = None,
        bearer_token: str | None = None,
        reconnect: UserReconnect | None = None,
    ) -> ClientSession:
        del upstream, auth, bearer_token, reconnect
        self.sessions_of.append(user_id)
        return self._session


async def test_the_admin_mcp_refresh_uses_the_sign_in_the_mcp_shows(
    tmp_path: Path,
) -> None:
    """The deputy holds the MCP's admin sign-in: the refresh reattaches
    with the deputy's, like the dashboard's, never the caller's."""
    config, mcp_servers = make_oauth_upstream_config()
    async with make_upstream_session() as session:
        manager = LiveUserSessionsClientManager(session)
        parts = await make_admin_parts(
            tmp_path, config=config, mcp_servers=mcp_servers,
            plan=PlanName.team, client_manager=manager,
            auth_coordinator=PendingAuthCoordinator(b"k" * 32),
        )
        store = parts.action_deps.connection_store
        assert store is not None
        await store.put_user_token(
            DEFAULT_ORG_ID, DEPUTY_EMAIL, "notion", make_stored_oauth_token(),
        )

        text = await _call(
            parts.server, "refresh_upstream_tools", {"mcp_id": "notion"},
        )

    assert text == "Refreshed 2 tools from upstream MCP 'notion'."
    assert manager.sessions_of == [DEPUTY_EMAIL]


# --- get_upstream ---


async def test_get_upstream_says_a_client_secret_is_set_without_its_value(
    tmp_path: Path,
) -> None:
    config, mcp_servers = make_oauth_upstream_config()
    config["upstreams"]["notion"]["client_id"] = "notion-client"
    config["upstreams"]["notion"]["client_secret"] = "s3cret-value"
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )

    text = await _call(parts.server, "get_upstream", {"mcp_id": "notion"})

    assert "s3cret-value" not in text
    auth = json.loads(text)["auth"]
    assert auth["client_id"] == "notion-client"
    assert auth["has_client_secret"] is True
    assert "client_secret" not in auth


async def test_get_upstream_does_not_return_the_service_account_token(
    tmp_path: Path,
) -> None:
    """A service-account MCP's credential is the bearer the gateway sends
    it, in ``auth.token`` and in its Authorization header. The AI client
    reading ``get_upstream`` gets neither, only that a token is set and
    the headers' names: every header value is hidden but references."""
    config = make_config_two_admins()
    config["upstreams"] = {
        "intranet": {"display_name": "Intranet", "auth_mode": "service_account"},
    }
    mcp_servers = {
        "intranet": {
            "url": "http://127.0.0.1:9001/mcp",
            "headers": {
                "Authorization": "Bearer svc-bearer-SUPERSECRET-123",
                "X-Tenant": "acme",
            },
        },
    }
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )

    text = await _call(parts.server, "get_upstream", {"mcp_id": "intranet"})

    assert "SUPERSECRET" not in text, text
    view = json.loads(text)
    assert view["auth"]["has_token"] is True
    assert "token" not in view["auth"]
    assert view["http"]["headers"] == {
        "Authorization": "[hidden]", "X-Tenant": "[hidden]",
    }


async def test_get_upstream_hides_credentials_typed_into_a_hosted_mcp(
    tmp_path: Path,
) -> None:
    """A credential typed in clear into an env var or a command-line
    argument is hidden too, and so is every other env value; a Variable
    reference (``${NAME}``, whose value the admin set as a password) and
    plain arguments are shown."""
    config = make_config_two_admins()
    config["upstreams"] = {"github": {"display_name": "GitHub"}}
    mcp_servers = {
        "github": {
            "command": "npx",
            "args": ["-y", "server-github", "--api-key", "plain-key-123"],
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
                "OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz0123",
                "LOG_LEVEL": "debug",
            },
        },
    }
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )

    text = await _call(parts.server, "get_upstream", {"mcp_id": "github"})

    assert "plain-key-123" not in text and "sk-abcdef" not in text, text
    stdio = json.loads(text)["stdio"]
    assert stdio["args"] == ["-y", "server-github", "--api-key", "[hidden]"]
    assert stdio["env"] == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
        "OPENAI_API_KEY": "[hidden]",
        "LOG_LEVEL": "[hidden]",
    }


ZAPIER_SECRET = "ZjQ5YTk3ZDItNjM4ZC00MzA0LWI2NjQtYjY5ZmJmNmI4ZTc1"


async def test_get_upstream_hides_the_credentials_of_a_url(tmp_path: Path) -> None:
    """A URL can carry a credential: a password (or a token alone) before
    the ``@``, a secret path handed out by a hosted MCP (Zapier), a
    cookie in a header whose name sounds harmless."""
    config = make_config_two_admins()
    config["upstreams"] = {"zapier": {"display_name": "Zapier"}}
    mcp_servers = {
        "zapier": {
            "url": f"https://bob:pa55word@mcp.zapier.com/api/mcp/s/{ZAPIER_SECRET}/mcp",
            "headers": {"Cookie": "session=abcd1234efgh5678ijkl9012"},
        },
    }
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )

    text = await _call(parts.server, "get_upstream", {"mcp_id": "zapier"})

    for secret in ("pa55word", ZAPIER_SECRET, "abcd1234efgh5678ijkl9012"):
        assert secret not in text, text
    http = json.loads(text)["http"]
    assert http["url"] == "https://[hidden]@mcp.zapier.com/api/mcp/s/[hidden]/mcp"
    assert http["headers"] == {"Cookie": "[hidden]"}


async def test_get_upstream_hides_the_credentials_of_a_command(
    tmp_path: Path,
) -> None:
    """A credential can sit in the command itself (an env var set in
    front of it), and in env vars whose names sound harmless: a database
    URL's password, a GitLab token under ``PAT``."""
    config = make_config_two_admins()
    config["upstreams"] = {"db": {"display_name": "DB"}}
    mcp_servers = {
        "db": {
            "command": "API_KEY=plain-key-123 node /srv/mcp/server.js",
            "env": {
                "DATABASE_URL": (
                    "postgres://admin:S3cretPassw0rd@db.example.com:5432/app"
                ),
                "PAT": "glpat-abcdefghij0123456789",
                "AUTH_MODE": "oauth",
            },
        },
    }
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers, plan=PlanName.team,
    )

    text = await _call(parts.server, "get_upstream", {"mcp_id": "db"})

    for secret in ("plain-key-123", "S3cretPassw0rd", "glpat-"):
        assert secret not in text, text
    stdio = json.loads(text)["stdio"]
    assert stdio["command"] == "API_KEY=[hidden] node /srv/mcp/server.js"
    assert stdio["env"] == {
        "DATABASE_URL": "[hidden]", "PAT": "[hidden]", "AUTH_MODE": "[hidden]",
    }
