"""Removing a teammate deletes their gateway logins and saved sign-ins.

When an admin removes a teammate on the Team page
(``DELETE /api/admin/users/{email}``), two kinds of per-person access
must go with them:

- their gateway logins: the bearer tokens their AI clients use to reach
  the MCP Hero gateway;
- their saved sign-ins to upstream MCPs: the per-user OAuth token, plus
  the client registration and server metadata saved with it (a leftover
  client registration makes a later re-invite fail with
  ``invalid_client``).

Another teammate's logins and saved sign-ins must stay untouched.

These tests drive the real standalone app. Gateway logins are minted
through the app's test-mode token endpoint, so they are real gateway
tokens; they are checked with the same token check the gateway runs on
every MCP request, and in the saved login file the gateway reloads after
a restart. Saved sign-ins are written to, and read back from, the app's
on-disk connection store by a separate store instance, which is what the
app sees after a restart.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import BaseModel

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.repositories.file_oauth_state_repository import (
    FileOAuthStateRepository,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._dev_stub_login import login_as
from tests.unit.factories import make_config_users_accepted

ADMIN = "admin@example.com"
ALICE = "alice@example.com"  # the teammate who gets removed
BOB = "bob@example.com"  # the teammate who stays
UPSTREAM = "notion"  # a remote MCP where each person signs in for themselves


def make_settings(tmp_path: Path) -> Settings:
    """Standalone settings: one admin, two teammates, one per-user
    sign-in MCP. ``test_mode`` turns on the gateway-login minting
    endpoint."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {UPSTREAM: {"url": "http://localhost:9001/mcp"}},
    }))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "upstreams": {
            UPSTREAM: {"display_name": "Notion", "auth_mode": "per_user_oauth"},
        },
        "roles": {
            "admin": {
                "is_admin": True,
                "settings": {"mcp_access": {"mcps": {UPSTREAM: True}}},
            },
            "member": {
                "is_default": True,
                "settings": {"mcp_access": {"mcps": {UPSTREAM: True}}},
            },
        },
        "users": {
            ADMIN: {"role": "admin"},
            ALICE: {"role": "member"},
            BOB: {"role": "member"},
        },
    }))
    data_dir = tmp_path / "data"
    # Every user accepted their invitation: a pending one gives no access.
    make_config_users_accepted(data_dir, config.read_text())
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
        test_mode=True,
    )


def make_admin_client(settings: Settings) -> TestClient:
    """A dashboard client signed in as the org admin."""
    client = TestClient(create_app(settings), raise_server_exceptions=True)
    login_as(client, ADMIN)
    return client


def make_saved_sign_in_token(access_token: str) -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        refresh_token=f"{access_token}-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["read"],
    )


def gateway_provider(client: TestClient) -> McpGatewayOAuthProvider:
    provider = client.app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined]
    assert isinstance(provider, McpGatewayOAuthProvider)
    return provider


def mint_gateway_login(client: TestClient, email: str) -> str:
    """Log ``email``'s AI client into the gateway; returns its bearer
    token (the endpoint also stores a matching refresh token)."""
    resp = client.post(
        "/api/auth/test-mcp-token",
        json={"email": email, "org_slug": "default"},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    assert isinstance(token, str)
    return token


def gateway_accepts(provider: McpGatewayOAuthProvider, bearer_token: str) -> bool:
    """The check the gateway runs on every MCP request's bearer token."""
    return asyncio.run(provider.verify_token(bearer_token)) is not None


def saved_gateway_login_owners(settings: Settings) -> set[str]:
    """Who holds a gateway login (access or refresh token) in the saved
    login file, which is what the gateway loads after a restart."""
    snapshot = asyncio.run(FileOAuthStateRepository(settings.data_dir).load())
    return (
        {token.user_email for token in snapshot.access_tokens.values()}
        | {token.user_email for token in snapshot.refresh_tokens.values()}
    )


class SavedSignIn(BaseModel):
    """What the connection store holds for one person on one MCP."""

    access_token: str | None
    client_info: dict[str, object] | None
    oauth_metadata: dict[str, object] | None


async def save_sign_in(store: FileConnectionStore, email: str) -> None:
    """Store a complete per-user sign-in for ``email`` on ``UPSTREAM``,
    as the upstream OAuth callback does."""
    await store.put_user_token(
        DEFAULT_ORG_ID, email, UPSTREAM, make_saved_sign_in_token(f"at-{email}"),
    )
    await store.put_client_info(
        DEFAULT_ORG_ID, UPSTREAM, email, {"client_id": f"cid-{email}"},
    )
    await store.put_oauth_metadata(
        DEFAULT_ORG_ID, UPSTREAM, email, {"issuer": "https://auth.example.invalid"},
    )


async def read_sign_in(store: FileConnectionStore, email: str) -> SavedSignIn:
    token = await store.get_user_token(DEFAULT_ORG_ID, email, UPSTREAM)
    return SavedSignIn(
        access_token=token.access_token if token is not None else None,
        client_info=await store.get_client_info(DEFAULT_ORG_ID, UPSTREAM, email),
        oauth_metadata=await store.get_oauth_metadata(DEFAULT_ORG_ID, UPSTREAM, email),
    )


def remove_teammate(client: TestClient, email: str) -> None:
    resp = client.delete(f"/api/admin/users/{email}")
    assert resp.status_code == 200, resp.text


def test_removing_a_teammate_locks_their_ai_clients_out_of_the_gateway(
    tmp_path: Path,
) -> None:
    """After removal, the removed teammate's gateway logins stop working,
    they no longer count as logged in to the gateway, and a restart does
    not bring their logins back; the other teammate's login keeps
    working."""
    settings = make_settings(tmp_path)
    client = make_admin_client(settings)
    provider = gateway_provider(client)
    alice_token = mint_gateway_login(client, ALICE)
    bob_token = mint_gateway_login(client, BOB)
    assert gateway_accepts(provider, alice_token)
    assert ALICE in provider.get_connected_users()
    assert {ALICE, BOB} <= saved_gateway_login_owners(settings)

    remove_teammate(client, ALICE)

    assert not gateway_accepts(provider, alice_token)
    # Covers the refresh token as well: the list counts a person who
    # holds a live access token OR a live refresh token.
    assert ALICE not in provider.get_connected_users()
    assert ALICE not in saved_gateway_login_owners(settings)
    assert gateway_accepts(provider, bob_token)
    assert BOB in provider.get_connected_users()
    assert BOB in saved_gateway_login_owners(settings)


def test_removing_a_teammate_deletes_their_saved_sign_ins(tmp_path: Path) -> None:
    """After removal, the removed teammate's saved sign-in to an upstream
    MCP is gone (token, client registration and server metadata), as
    seen by the store after a restart; the other teammate's saved
    sign-in is untouched."""
    settings = make_settings(tmp_path)
    client = make_admin_client(settings)
    asyncio.run(save_sign_in(FileConnectionStore(settings.data_dir), ALICE))
    asyncio.run(save_sign_in(FileConnectionStore(settings.data_dir), BOB))
    before = asyncio.run(read_sign_in(FileConnectionStore(settings.data_dir), ALICE))
    assert before.access_token == f"at-{ALICE}"

    remove_teammate(client, ALICE)

    after_restart = FileConnectionStore(settings.data_dir)
    assert asyncio.run(read_sign_in(after_restart, ALICE)) == SavedSignIn(
        access_token=None, client_info=None, oauth_metadata=None,
    )
    assert asyncio.run(read_sign_in(after_restart, BOB)) == SavedSignIn(
        access_token=f"at-{BOB}",
        client_info={"client_id": f"cid-{BOB}"},
        oauth_metadata={"issuer": "https://auth.example.invalid"},
    )
