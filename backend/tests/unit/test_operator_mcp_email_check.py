"""Who may use the operator MCP (``/admin-mcp/system``).

The operator MCP carries cross-org tools. Only the emails listed in
``MCPOLIS_SUPERADMIN_EMAILS`` may use it. The gate built by
``_build_superadmin_app_with_oauth`` (``app.py``) works in two steps:

- no valid gateway sign-in -> 401, with the ``WWW-Authenticate`` header
  that tells the MCP client where to sign in;
- a valid sign-in whose email is not in the list (ASCII letter case
  ignored, otherwise exact) -> 403
  ``{"error": "Not a superadmin"}``.

The gate is built through the real builder, with the real gateway OAuth
provider issuing the sign-ins and the operator list parsed by the real
``Settings.parsed_superadmin_emails`` (what production passes). Only the
operator MCP behind the gate is a stub, which records who reached it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.routing import Mount, Route

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from mcpolis.adapters.repositories.file_oauth_state_repository import (
    FileOAuthStateRepository,
)
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.app import _build_superadmin_app_with_oauth
from mcpolis.entrypoints.config import Settings
from tests.unit.factories import make_runtime_manager

SERVER_URL = "http://localhost:8000"
OPERATOR_MCP_PATH = "/admin-mcp/system"
OPERATOR_EMAIL = "ops@example.com"
SECOND_OPERATOR_EMAIL = "second-ops@example.com"
# Written the way an operator writes the env var: comma-separated, spaces.
OPERATOR_EMAILS_SETTING = f" {OPERATOR_EMAIL} , {SECOND_OPERATOR_EMAIL} "
TEAM_MEMBER_EMAIL = "alice@example.com"
INITIALIZE_REQUEST: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}


def make_settings(superadmin_emails: str = OPERATOR_EMAILS_SETTING) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        server_url=SERVER_URL,
        superadmin_emails=superadmin_emails,
    )


def make_gateway_provider(tmp_path: Path) -> McpGatewayOAuthProvider:
    """The real gateway OAuth provider: it issues the sign-ins the gate
    checks, and its tokens carry the signed-in email."""
    return McpGatewayOAuthProvider(
        google_client_id="",
        google_client_secret="",
        server_url=SERVER_URL,
        runtime_manager=make_runtime_manager(PolicyEngine(SettingsConfig())),
        state_repository=FileOAuthStateRepository(tmp_path / "data"),
    )


class OperatorMcpStub(FastMCP):
    """Stands in for the operator MCP behind the gate: its HTTP app
    answers 200 and records the email of every caller that got through.
    The real session manager is still built, because the gate wraps the
    app in the session-owner check that reads it."""

    def __init__(self, reached_by: list[str]) -> None:
        super().__init__("operator-mcp-stub")
        self._reached_by = reached_by

    def streamable_http_app(self) -> Starlette:
        super().streamable_http_app()
        reached_by = self._reached_by

        async def operator_mcp(_request: Request) -> JSONResponse:
            user = auth_context_var.get()
            reached_by.append(user.display_name if user is not None else "")
            return JSONResponse({"operator_mcp": "reached"})

        return Starlette(
            routes=[Route("/", endpoint=operator_mcp, methods=["POST"])],
        )


def make_operator_mcp_client(
    provider: McpGatewayOAuthProvider,
    reached_by: list[str],
    *,
    superadmin_emails: str = OPERATOR_EMAILS_SETTING,
) -> TestClient:
    """Mount the gated operator MCP at the path production mounts it on."""
    settings = make_settings(superadmin_emails)
    gated = _build_superadmin_app_with_oauth(
        OperatorMcpStub(reached_by),
        provider,
        settings,
        settings.parsed_superadmin_emails(),
    )
    parent = Starlette(routes=[Mount(OPERATOR_MCP_PATH, app=gated)])
    return TestClient(parent, raise_server_exceptions=True)


def call_operator_mcp(client: TestClient, headers: dict[str, str]) -> Response:
    return client.post(
        f"{OPERATOR_MCP_PATH}/", headers=headers, json=INITIALIZE_REQUEST,
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "email", [OPERATOR_EMAIL, SECOND_OPERATOR_EMAIL], ids=["first", "second"],
)
async def test_listed_operator_email_reaches_the_operator_mcp(
    tmp_path: Path, email: str,
) -> None:
    """Every email in MCPOLIS_SUPERADMIN_EMAILS (comma-separated, spaces
    allowed) can use the operator MCP once signed in to the gateway."""
    provider = make_gateway_provider(tmp_path)
    token = await provider.mint_test_token(email)
    reached_by: list[str] = []
    client = make_operator_mcp_client(provider, reached_by)

    resp = call_operator_mcp(client, bearer(token))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"operator_mcp": "reached"}
    assert reached_by == [email]


@pytest.mark.parametrize(
    ("listed", "signed_in"),
    [("Ops@Example.com", "ops@example.com"), ("ops@example.com", "OPS@Example.COM")],
    ids=["list-has-capitals", "sign-in-has-capitals"],
)
async def test_operator_email_matches_whatever_its_letter_case(
    tmp_path: Path, listed: str, signed_in: str,
) -> None:
    """Google treats addresses as case-insensitive, so the operator list
    does too: listed as ``Ops@Example.com``, the operator who signs in as
    ``ops@example.com`` gets in, and the other way round."""
    provider = make_gateway_provider(tmp_path)
    token = await provider.mint_test_token(signed_in)
    reached_by: list[str] = []
    client = make_operator_mcp_client(
        provider, reached_by, superadmin_emails=listed,
    )

    resp = call_operator_mcp(client, bearer(token))

    assert resp.status_code == 200, resp.text
    assert reached_by == [signed_in]


async def test_signed_in_email_not_on_the_operator_list_is_refused(
    tmp_path: Path,
) -> None:
    """A valid gateway sign-in is not enough: an email missing from
    MCPOLIS_SUPERADMIN_EMAILS gets 403 and never reaches the operator MCP."""
    provider = make_gateway_provider(tmp_path)
    token = await provider.mint_test_token(TEAM_MEMBER_EMAIL)
    reached_by: list[str] = []
    client = make_operator_mcp_client(provider, reached_by)

    resp = call_operator_mcp(client, bearer(token))

    assert resp.status_code == 403
    assert resp.json() == {"error": "Not a superadmin"}
    assert reached_by == []


@pytest.mark.parametrize(
    "email",
    [f"evil-{OPERATOR_EMAIL}", f"{OPERATOR_EMAIL}.attacker.example"],
    ids=["contains-a-listed-email", "extends-a-listed-email"],
)
async def test_email_that_only_resembles_a_listed_email_is_refused(
    tmp_path: Path, email: str,
) -> None:
    """The list is matched email by email, exactly: an email that contains
    or extends a listed one is refused like any other."""
    provider = make_gateway_provider(tmp_path)
    token = await provider.mint_test_token(email)
    reached_by: list[str] = []
    client = make_operator_mcp_client(provider, reached_by)

    resp = call_operator_mcp(client, bearer(token))

    assert resp.status_code == 403
    assert resp.json() == {"error": "Not a superadmin"}
    assert reached_by == []


@pytest.mark.parametrize(
    "headers",
    [{}, bearer("not-a-gateway-token")],
    ids=["no-sign-in", "unknown-token"],
)
async def test_caller_without_a_valid_sign_in_is_sent_to_sign_in(
    tmp_path: Path, headers: dict[str, str],
) -> None:
    """No sign-in, or a token the gateway never issued: 401 with the header
    that points the MCP client at the operator MCP's sign-in metadata, and
    the operator MCP is never reached."""
    reached_by: list[str] = []
    client = make_operator_mcp_client(make_gateway_provider(tmp_path), reached_by)

    resp = call_operator_mcp(client, headers)

    assert resp.status_code == 401
    metadata_url = (
        f"{SERVER_URL}{OPERATOR_MCP_PATH}/.well-known/oauth-protected-resource"
    )
    assert f'resource_metadata="{metadata_url}"' in resp.headers["www-authenticate"]
    assert reached_by == []
