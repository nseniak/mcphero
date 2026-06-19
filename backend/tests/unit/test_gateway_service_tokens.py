"""Full-app gateway tests for service tokens (standalone mode).

Spins up a fake upstream + the real gateway on loopback ports (same
harness as ``test_mcp_integration.py``) and connects with
``svct_``-prefixed bearers minted into the same registry file the
app reads. Pins:

- tools/list reflects the token's role (not ``config.users``);
- a no-access role gets zero tools and denied calls;
- a deleted role fails closed;
- revocation bites on the next connection;
- the audit log records the ``svc:<label>`` identity.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import cast

import pytest
import uvicorn
from fastapi import FastAPI
from mcp.client.session import ClientSession

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import ACCESS_TOKEN_TTL
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._loopback_mcp import (
    await_tools_ready,
    free_ports,
    mcp_session_call,
    wait_for_health,
)

# OS-assigned at stack boot (see ``_start_stack``). Fixed ports collide
# under load — a fixed loopback connect that times out raised an uncaught
# ``httpx.ConnectTimeout`` and failed the test. ``_list_tools_with`` /
# ``_call_tool_with`` read these module globals, so the file's single
# serial stack sets them before the helpers run.
_gateway_port = 0
_upstream_port = 0


def _write_config(tmp_path: Path, upstream_port: int) -> tuple[Path, Path]:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "fake": {"url": f"http://127.0.0.1:{upstream_port}/mcp"},
        },
    }))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "upstreams": {
            "fake": {"display_name": "Fake", "auth_mode": "service_account"},
        },
        "roles": {
            "admin": {
                "is_admin": True,
                "settings": {"mcp_access": {"mcps": {"fake": True}}},
            },
            "full": {
                "settings": {"mcp_access": {"mcps": {"fake": True}}},
            },
            "none": {
                "settings": {"mcp_access": {"mcps": {}}},
            },
        },
        "users": {
            "admin@example.com": {"role": "admin"},
        },
    }))
    return mcp_json, config


def _create_fake_upstream():  # noqa: ANN202 — FastMCP type lives in test dep
    from mcp.server.fastmcp import FastMCP

    fake = FastMCP("fake", host="127.0.0.1", port=_upstream_port)

    @fake.tool(description="Say hello")
    def greet(name: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return f"Hello, {name}"

    return fake


async def _start_stack(
    tmp_path: Path,
) -> tuple[uvicorn.Server, uvicorn.Server, asyncio.Task[None], FastAPI]:
    global _gateway_port, _upstream_port
    _upstream_port, _gateway_port = free_ports(2)
    fake_mcp = _create_fake_upstream()
    upstream_server = uvicorn.Server(uvicorn.Config(
        fake_mcp.streamable_http_app(), host="127.0.0.1",
        port=_upstream_port, log_level="warning", ws="none",
    ))

    mcp_json_path, config_path = _write_config(tmp_path, _upstream_port)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        host="127.0.0.1",
        port=_gateway_port,
        mcp_json_path=mcp_json_path,
        config_path=config_path,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        test_mode=True,
        google_client_id="",
        google_client_secret="",
        session_secret="svc-token-test-secret",
        server_url=f"http://127.0.0.1:{_gateway_port}",
    )
    # Hold the app object so tests can mint a human OAuth bearer
    # (``mint_test_token``) and reach the live provider for AUTH-9/10/11.
    gateway_app = create_app(settings)
    gateway_server = uvicorn.Server(uvicorn.Config(
        gateway_app, host="127.0.0.1", port=_gateway_port,
        log_level="warning", ws="none",
    ))

    async def run_servers() -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(upstream_server.serve())
            tg.create_task(gateway_server.serve())

    server_task = asyncio.create_task(run_servers())
    await wait_for_health(
        f"http://127.0.0.1:{_upstream_port}/health",
        f"http://127.0.0.1:{_gateway_port}/health",
        label="service-token gateway stack",
    )
    return upstream_server, gateway_server, server_task, gateway_app


async def _stop_stack(
    upstream_server: uvicorn.Server,
    gateway_server: uvicorn.Server,
    server_task: asyncio.Task[None],
) -> None:
    upstream_server.should_exit = True
    gateway_server.should_exit = True
    await asyncio.sleep(0.3)
    server_task.cancel()
    try:
        await server_task
    except (asyncio.CancelledError, ExceptionGroup):
        pass


def make_registry_service(tmp_path: Path) -> ServiceTokenService:
    """Sibling service over the same registry file the app reads.

    ``FileServiceTokenRepository`` re-reads the JSON on every call, so
    tokens minted here are immediately visible to the running gateway
    — the verify path under test is the app's own.
    """
    return ServiceTokenService(
        repo=FileServiceTokenRepository(tmp_path / "data"),
    )


async def _list_tools_with(token: str) -> list[str]:
    async def _list(session: ClientSession) -> list[str]:
        result = await session.list_tools()
        return [t.name for t in result.tools]

    return await mcp_session_call(
        f"http://127.0.0.1:{_gateway_port}/mcp/", token, _list,
    )


async def _call_tool_with(
    token: str, tool_name: str, arguments: dict[str, str],
) -> tuple[bool, str]:
    async def _call(session: ClientSession) -> tuple[bool, str]:
        result = await session.call_tool(tool_name, arguments)
        text = cast(str, result.content[0].text)  # type: ignore[union-attr]
        return result.isError or False, text

    return await mcp_session_call(
        f"http://127.0.0.1:{_gateway_port}/mcp/", token, _call,
    )


@pytest.mark.asyncio
async def test_service_token_gateway_end_to_end(tmp_path: Path) -> None:
    """One stack boot, every gateway-side service-token behavior."""
    upstream_server, gateway_server, server_task, _app = await _start_stack(
        tmp_path,
    )
    registry = make_registry_service(tmp_path)
    try:
        full = await registry.mint(
            org_id="default", label="full-bot", role_name="full",
            created_by="admin@example.com",
        )
        none = await registry.mint(
            org_id="default", label="none-bot", role_name="none",
            created_by="admin@example.com",
        )
        ghost = await registry.mint(
            org_id="default", label="ghost-bot", role_name="deleted-role",
            created_by="admin@example.com",
        )

        # Wait for the gateway->upstream connect to settle before the
        # first tools read — otherwise tools/list returns [] under load
        # and the assertion races it ("assert [] == ['fake__greet']").
        await await_tools_ready(
            f"http://127.0.0.1:{_gateway_port}/mcp/",
            full.raw_token, "fake__greet",
        )
        # Role-driven discovery: svc identity is not in config.users.
        assert await _list_tools_with(full.raw_token) == ["fake__greet"]

        # Allowed call goes through and is audited under svc:<label>.
        # NOTE: assert on text, not isError — the SDK drops the
        # handler's isError flag on the wire (same convention as
        # test_mcp_integration.py's policy tests).
        _is_error, text = await _call_tool_with(
            full.raw_token, "fake__greet", {"name": "World"},
        )
        assert "Hello, World" in text
        assert "Access denied" not in text

        # No-access role: zero tools, denied call.
        assert await _list_tools_with(none.raw_token) == []
        _is_error, text = await _call_tool_with(
            none.raw_token, "fake__greet", {"name": "World"},
        )
        assert "Access denied" in text

        # Deleted role fails closed (token authenticates, no access).
        assert await _list_tools_with(ghost.raw_token) == []

        # Unknown / revoked tokens never authenticate.
        with pytest.raises(BaseException):
            await _list_tools_with("svct_never-minted")
        await registry.revoke("default", "full-bot")
        with pytest.raises(BaseException):
            await _list_tools_with(full.raw_token)

        # Audit rows carry the svc identity.
        audit_path = tmp_path / "data" / "audit.jsonl"
        entries = [
            json.loads(line)
            for line in audit_path.read_text().strip().splitlines()
        ]
        tool_calls = [e for e in entries if e.get("tool") == "fake__greet"]
        assert any(
            e["user_id"] == "svc:full-bot"
            and e["response_status"] == "success"
            for e in tool_calls
        )
        connects = [e for e in entries if e.get("action") == "client_connect"]
        assert any(e["user_id"] == "svc:full-bot" for e in connects)
    finally:
        await _stop_stack(upstream_server, gateway_server, server_task)


# ─────────────── AUTH-9 / AUTH-10 / AUTH-11 (live gateway) ──────────────


@pytest.mark.asyncio
async def test_oauth_and_service_token_auth_coexist(tmp_path: Path) -> None:
    """AUTH-9 — both auth modes live in one app.

    A human OAuth bearer (minted via the provider's test-token path) and
    a ``svct_`` bearer hit the same gateway. The OAuth bearer resolves
    via the OAuth store to the human's ``config.users`` role; the svct_
    resolves via the registry to ``svc:<label>`` with its minted role.
    Neither path leaks into the other.
    """
    upstream_server, gateway_server, server_task, gateway_app = (
        await _start_stack(tmp_path)
    )
    registry = make_registry_service(tmp_path)
    try:
        provider = gateway_app.state.mcp_gateway_oauth_provider
        oauth_token = await provider.mint_test_token("admin@example.com")
        svc = await registry.mint(
            org_id="default", label="full-bot", role_name="full",
            created_by="admin@example.com",
        )
        await await_tools_ready(
            f"http://127.0.0.1:{_gateway_port}/mcp/",
            svc.raw_token, "fake__greet",
        )

        # Human OAuth bearer: admin role → sees the tool, audited as email.
        assert await _list_tools_with(oauth_token) == ["fake__greet"]
        _is_err, oauth_text = await _call_tool_with(
            oauth_token, "fake__greet", {"name": "Human"},
        )
        assert "Hello, Human" in oauth_text

        # svct_ bearer: registry role → sees the tool, audited as svc.
        assert await _list_tools_with(svc.raw_token) == ["fake__greet"]
        _is_err, svc_text = await _call_tool_with(
            svc.raw_token, "fake__greet", {"name": "Bot"},
        )
        assert "Hello, Bot" in svc_text

        # Audit rows pin the distinct identities to the distinct paths.
        audit_path = tmp_path / "data" / "audit.jsonl"
        entries = [
            json.loads(line)
            for line in audit_path.read_text().strip().splitlines()
        ]
        tool_calls = [e for e in entries if e.get("tool") == "fake__greet"]
        identities = {e["user_id"] for e in tool_calls}
        assert "admin@example.com" in identities  # OAuth path
        assert "svc:full-bot" in identities       # registry path
    finally:
        await _stop_stack(upstream_server, gateway_server, server_task)


@pytest.mark.asyncio
async def test_service_token_revoked_mid_session_fails_next_request(
    tmp_path: Path,
) -> None:
    """AUTH-10 — revocation bites on the next request.

    A live svct_ lists tools fine; after ``revoke`` the token no longer
    authenticates (per-request verification), so the next connect fails —
    not a silent success on a cached session.
    """
    upstream_server, gateway_server, server_task, _app = await _start_stack(
        tmp_path,
    )
    registry = make_registry_service(tmp_path)
    try:
        svc = await registry.mint(
            org_id="default", label="full-bot", role_name="full",
            created_by="admin@example.com",
        )
        await await_tools_ready(
            f"http://127.0.0.1:{_gateway_port}/mcp/",
            svc.raw_token, "fake__greet",
        )
        assert await _list_tools_with(svc.raw_token) == ["fake__greet"]

        # Revoke, then a fresh request must fail auth (no cached pass).
        assert await registry.revoke("default", "full-bot") is True
        with pytest.raises(BaseException):
            await _list_tools_with(svc.raw_token)
    finally:
        await _stop_stack(upstream_server, gateway_server, server_task)


@pytest.mark.asyncio
async def test_expired_oauth_bearer_is_rejected(tmp_path: Path) -> None:
    """AUTH-11 — an expired OAuth bearer on /mcp fails to authenticate.

    Mint a gateway token, then advance time past ``ACCESS_TOKEN_TTL`` by
    rewriting the stored token's ``expires_at`` into the past (the
    provider reads ``int(time.time())`` on every ``load_access_token``).
    The next connect must fail — ``load_access_token`` pops the expired
    entry and returns ``None``, so ``BearerAuthBackend`` rejects it.
    """
    upstream_server, gateway_server, server_task, gateway_app = (
        await _start_stack(tmp_path)
    )
    try:
        provider = gateway_app.state.mcp_gateway_oauth_provider
        token = await provider.mint_test_token("admin@example.com")
        # Sanity: valid before expiry.
        assert await _list_tools_with(token) == ["fake__greet"]

        # Simulate the clock advancing past ACCESS_TOKEN_TTL: the stored
        # token's expiry moves into the past.
        await provider._ensure_loaded()
        stored = provider._access_tokens[token]
        stored.expires_at = int(time.time()) - (ACCESS_TOKEN_TTL + 1)

        with pytest.raises(BaseException):
            await _list_tools_with(token)
    finally:
        await _stop_stack(upstream_server, gateway_server, server_task)


# ─────────────── AUTH-13 / AUTH-14 (behavioral auth backend) ────────────


@pytest.mark.asyncio
async def test_bearer_auth_backend_accepts_minted_service_token(
    tmp_path: Path,
) -> None:
    """AUTH-13 — behavioral ``BearerAuthBackend`` contract.

    Replaces the brittle source-grep in
    ``test_service_token_verifier.py`` (which asserts on the SDK's source
    text) with a behavioral check: construct a real
    ``BearerAuthBackend`` over the composite verifier and drive its
    ``authenticate`` with a minted ``svct_`` bearer (``expires_at=None``,
    i.e. non-expiring). It must return an ``AuthenticatedUser`` carrying
    the ``svc:<label>`` identity and the service-token scopes — proving
    the SDK's truthiness expiry check treats ``None`` as non-expiring.
    """
    from mcp.server.auth.middleware.bearer_auth import (
        AuthenticatedUser,
        BearerAuthBackend,
    )
    from starlette.requests import HTTPConnection

    from mcpolis.adapters.auth.service_token_verifier import (
        CompositeGatewayTokenVerifier,
        ServiceTokenVerifier,
    )

    service = make_registry_service(tmp_path)
    minted = await service.mint(
        org_id="default", label="ci-bot", role_name="reader",
        created_by="admin@example.com",
    )

    class _RejectingOAuth:
        async def verify_token(self, token: str) -> None:
            raise AssertionError("svct_ must not reach the OAuth path")

    backend = BearerAuthBackend(
        CompositeGatewayTokenVerifier(
            ServiceTokenVerifier(service), _RejectingOAuth(),  # type: ignore[arg-type]
        ),
    )
    conn = HTTPConnection({
        "type": "http",
        "headers": [(b"authorization", f"Bearer {minted.raw_token}".encode())],
    })
    result = await backend.authenticate(conn)
    assert result is not None
    _creds, user = result
    assert isinstance(user, AuthenticatedUser)
    assert user.display_name == "svc:ci-bot"
    assert "mcpolis:svc" in user.access_token.scopes


async def _initialize_status_with_auth(
    auth_value: str | None,
) -> int:
    """POST an MCP ``initialize`` to ``/mcp/`` with a raw Authorization
    header value (or none) and return the HTTP status code.

    Uses the streamable-HTTP transport's own initialize shape so the
    request reaches the auth middleware exactly as a real client's would.
    """
    import httpx as _httpx

    headers: dict[str, str] = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if auth_value is not None:
        headers["Authorization"] = auth_value
    async with _httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{_gateway_port}/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0"},
                },
            },
        )
    return resp.status_code


@pytest.mark.asyncio
async def test_malformed_authorization_headers_rejected(tmp_path: Path) -> None:
    """AUTH-14 — malformed Authorization credentials 401 without crashing.

    A no-scheme token, a wrong scheme, an empty bearer value, and a bare
    garbage bearer are each presented to the live gateway. Each must
    return 401 (failed to authenticate) — never a 5xx, never a hang —
    proving the auth boundary handles junk credentials, not just absent
    ones.
    """
    upstream_server, gateway_server, server_task, _app = await _start_stack(
        tmp_path,
    )
    try:
        cases: list[str | None] = [
            None,                 # no header at all (baseline)
            "svct_x",             # no scheme
            "Basic Zm9vOmJhcg==",  # wrong scheme
            "Bearer",             # scheme, no token
            "Bearer not-a-real-token",  # garbage bearer
        ]
        for auth_value in cases:
            status = await _initialize_status_with_auth(auth_value)
            # Failed to authenticate (4xx), never a server crash (5xx).
            assert status == 401, (auth_value, status)
    finally:
        await _stop_stack(upstream_server, gateway_server, server_task)
