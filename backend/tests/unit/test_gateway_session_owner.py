"""Every gateway session belongs to the caller that opened it.

The MCP SDK runs each session's requests inside the task it started at
``initialize``, so a handler sees the identity (and org) captured then,
not the bearer on the current request. Without an owner check, any
authenticated caller who learned another caller's ``mcp-session-id``
(it appears in ``client_connect`` audit rows and in logs) acted as that
session's creator: a member listed and called a service token's tools,
kept using the session after the token was revoked, and could end it.

These full-app tests (real gateway + fake upstream on loopback, the
``test_gateway_service_tokens.py`` harness) pin the fix: a request on
someone else's session gets exactly the answer an unknown session id
gets (404), and the owner's own requests keep working.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from starlette.routing import Mount, Route

from mcpolis.entrypoints.middleware.session_owner_guard import SessionOwnerGuard
from tests.unit._loopback_mcp import await_tools_ready
from tests.unit.test_gateway_service_tokens import (
    _start_stack,
    _stop_stack,
    make_registry_service,
)

ADMIN = "admin@example.com"
MEMBER = "member@example.com"  # role "none" in the harness config
PROTOCOL_VERSION = "2025-06-18"


def make_gateway_url(gateway_server: uvicorn.Server) -> str:
    return f"http://127.0.0.1:{gateway_server.config.port}/mcp/"


def make_headers(bearer: str, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


async def open_session(
    client: httpx.AsyncClient, url: str, bearer: str,
) -> str:
    """``initialize`` + ``notifications/initialized``; returns the id."""
    init = await client.post(
        url,
        headers=make_headers(bearer),
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "session-owner-test", "version": "0"},
            },
        },
    )
    assert init.status_code == 200, init.text
    session_id = init.headers["mcp-session-id"]
    ack = await client.post(
        url,
        headers=make_headers(bearer, session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert ack.status_code == 202, ack.text
    return session_id


async def send_request(
    client: httpx.AsyncClient,
    url: str,
    bearer: str,
    session_id: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    return await client.post(
        url,
        headers=make_headers(bearer, session_id),
        json={
            "jsonrpc": "2.0", "id": 2, "method": method,
            "params": params or {},
        },
    )


def rpc_result(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC ``result`` of a 200 answer (SSE or plain JSON)."""
    assert response.status_code == 200, (response.status_code, response.text)
    if response.headers.get("content-type", "").startswith(
        "text/event-stream",
    ):
        messages = [
            json.loads(line[len("data:"):])
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
    else:
        messages = [response.json()]
    answers = [m for m in messages if m.get("id") == 2]
    assert len(answers) == 1, response.text
    return answers[0]["result"]


def tool_names(response: httpx.Response) -> list[str]:
    return [tool["name"] for tool in rpc_result(response)["tools"]]


def find_guard(app: FastAPI, mount_path: str) -> SessionOwnerGuard:
    """The ``SessionOwnerGuard`` behind the ``/`` route of ``mount_path``."""
    for mount in app.routes:
        if not (isinstance(mount, Mount) and mount.path == mount_path):
            continue
        for route in mount.routes:
            if isinstance(route, Route) and route.path == "/":
                node: Any = route.endpoint
                while not isinstance(node, SessionOwnerGuard):
                    node = node.app
                return node
    raise AssertionError(f"no SessionOwnerGuard under {mount_path}")


@pytest.mark.asyncio
async def test_member_cannot_use_a_service_token_session(
    tmp_path: Path,
) -> None:
    """A member's bearer on a service token's session id: no tools, no
    calls. The member's own session lists zero tools, the token's
    session would have listed the token's ``full`` role tools."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        svc = await make_registry_service(tmp_path).mint(
            org_id="default", label="full-bot", role_name="full",
            created_by=ADMIN,
        )
        await await_tools_ready(url, svc.raw_token, "fake__greet")
        member = await app.state.mcp_gateway_oauth_provider.mint_test_token(
            MEMBER,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            svc_session = await open_session(client, url, svc.raw_token)
            member_session = await open_session(client, url, member)
            assert tool_names(await send_request(
                client, url, member, member_session, "tools/list",
            )) == []

            listed = await send_request(
                client, url, member, svc_session, "tools/list",
            )
            assert listed.status_code == 404, listed.text
            called = await send_request(
                client, url, member, svc_session, "tools/call",
                {"name": "fake__greet", "arguments": {"name": "Mallory"}},
            )
            assert called.status_code == 404, called.text

            # The owner's session is untouched.
            assert tool_names(await send_request(
                client, url, svc.raw_token, svc_session, "tools/list",
            )) == ["fake__greet"]
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_member_cannot_use_an_admin_session(tmp_path: Path) -> None:
    """Same rule between two people: a member's bearer on an admin's
    session id does not get the admin's tools."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        provider = app.state.mcp_gateway_oauth_provider
        admin = await provider.mint_test_token(ADMIN)
        member = await provider.mint_test_token(MEMBER)
        await await_tools_ready(url, admin, "fake__greet")
        async with httpx.AsyncClient(timeout=30.0) as client:
            admin_session = await open_session(client, url, admin)

            listed = await send_request(
                client, url, member, admin_session, "tools/list",
            )
            assert listed.status_code == 404, listed.text

            assert tool_names(await send_request(
                client, url, admin, admin_session, "tools/list",
            )) == ["fake__greet"]
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_revoked_service_token_session_is_dead_for_everyone(
    tmp_path: Path,
) -> None:
    """After revocation the token itself is refused (401) and nobody
    else can pick its session up either (404)."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    registry = make_registry_service(tmp_path)
    try:
        svc = await registry.mint(
            org_id="default", label="full-bot", role_name="full",
            created_by=ADMIN,
        )
        await await_tools_ready(url, svc.raw_token, "fake__greet")
        member = await app.state.mcp_gateway_oauth_provider.mint_test_token(
            MEMBER,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            svc_session = await open_session(client, url, svc.raw_token)
            assert await registry.revoke("default", "full-bot") is True

            by_token = await send_request(
                client, url, svc.raw_token, svc_session, "tools/list",
            )
            assert by_token.status_code == 401, by_token.text
            by_member = await send_request(
                client, url, member, svc_session, "tools/list",
            )
            assert by_member.status_code == 404, by_member.text
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_member_cannot_end_or_stream_a_service_token_session(
    tmp_path: Path,
) -> None:
    """DELETE and the GET event stream are owner-only too. A refused
    DELETE leaves the session working for its owner."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        svc = await make_registry_service(tmp_path).mint(
            org_id="default", label="full-bot", role_name="full",
            created_by=ADMIN,
        )
        await await_tools_ready(url, svc.raw_token, "fake__greet")
        member = await app.state.mcp_gateway_oauth_provider.mint_test_token(
            MEMBER,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            svc_session = await open_session(client, url, svc.raw_token)

            async with client.stream(
                "GET", url,
                headers={
                    **make_headers(member, svc_session),
                    "Accept": "text/event-stream",
                },
            ) as stream:
                stream_status = stream.status_code
            assert stream_status == 404

            ended = await client.delete(
                url, headers=make_headers(member, svc_session),
            )
            assert ended.status_code == 404, ended.text

            assert tool_names(await send_request(
                client, url, svc.raw_token, svc_session, "tools/list",
            )) == ["fake__greet"]
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_new_token_with_the_same_label_cannot_use_the_old_session(
    tmp_path: Path,
) -> None:
    """A service token owns its session, not its label: a token re-minted
    under the revoked one's label (here with a no-access role) must not
    inherit the old session and the old role with it."""
    upstream, gateway, task, _app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    registry = make_registry_service(tmp_path)
    try:
        old = await registry.mint(
            org_id="default", label="bot", role_name="full",
            created_by=ADMIN,
        )
        await await_tools_ready(url, old.raw_token, "fake__greet")
        async with httpx.AsyncClient(timeout=30.0) as client:
            old_session = await open_session(client, url, old.raw_token)
            assert await registry.revoke("default", "bot") is True
            new = await registry.mint(
                org_id="default", label="bot", role_name="none",
                created_by=ADMIN,
            )

            listed = await send_request(
                client, url, new.raw_token, old_session, "tools/list",
            )
            assert listed.status_code == 404, listed.text
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_foreign_session_gets_the_unknown_session_answer(
    tmp_path: Path,
) -> None:
    """Anti-enumeration: someone else's live session id and an id that
    never existed get the same status, type and body, so the answer
    can't be used to learn which session ids are live."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        provider = app.state.mcp_gateway_oauth_provider
        admin = await provider.mint_test_token(ADMIN)
        member = await provider.mint_test_token(MEMBER)
        async with httpx.AsyncClient(timeout=30.0) as client:
            admin_session = await open_session(client, url, admin)

            foreign = await send_request(
                client, url, member, admin_session, "tools/list",
            )
            unknown = await send_request(
                client, url, member, uuid.uuid4().hex, "tools/list",
            )
            assert foreign.status_code == unknown.status_code == 404
            assert foreign.headers.get("content-type") == (
                unknown.headers.get("content-type")
            )
            assert foreign.text == unknown.text
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_owner_keeps_its_session_with_a_new_access_token(
    tmp_path: Path,
) -> None:
    """A person owns the session, not one access token: a refreshed
    bearer for the same email keeps using it (a refresh must not cut a
    client off mid-session)."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        provider = app.state.mcp_gateway_oauth_provider
        first = await provider.mint_test_token(ADMIN)
        second = await provider.mint_test_token(ADMIN)
        assert first != second
        await await_tools_ready(url, first, "fake__greet")
        async with httpx.AsyncClient(timeout=30.0) as client:
            session = await open_session(client, url, first)
            assert tool_names(await send_request(
                client, url, second, session, "tools/list",
            )) == ["fake__greet"]
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_owner_opens_the_event_stream(tmp_path: Path) -> None:
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        admin = await app.state.mcp_gateway_oauth_provider.mint_test_token(
            ADMIN,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            session = await open_session(client, url, admin)
            async with client.stream(
                "GET", url,
                headers={
                    **make_headers(admin, session),
                    "Accept": "text/event-stream",
                },
            ) as stream:
                status = stream.status_code
                content_type = stream.headers.get("content-type", "")
            assert status == 200
            assert content_type.startswith("text/event-stream")
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_two_session_headers_are_read_as_the_sdk_reads_them(
    tmp_path: Path,
) -> None:
    """With two ``mcp-session-id`` headers, the guard and the SDK both act
    on the first one: a victim's id can't be checked in one place and
    served from the other."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        provider = app.state.mcp_gateway_oauth_provider
        admin = await provider.mint_test_token(ADMIN)
        member = await provider.mint_test_token(MEMBER)
        await await_tools_ready(url, admin, "fake__greet")
        async with httpx.AsyncClient(timeout=30.0) as client:
            admin_session = await open_session(client, url, admin)
            member_session = await open_session(client, url, member)
            tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

            def make_two_session_headers(
                first: str, second: str,
            ) -> list[tuple[str, str]]:
                return [
                    *make_headers(member).items(),
                    ("mcp-session-id", first),
                    ("mcp-session-id", second),
                ]

            victim_first = await client.post(
                url,
                headers=make_two_session_headers(admin_session, member_session),
                json=tools_list,
            )
            assert victim_first.status_code == 404, victim_first.text
            own_first = await client.post(
                url,
                headers=make_two_session_headers(member_session, admin_session),
                json=tools_list,
            )
            assert tool_names(own_first) == []  # the member's, not the admin's
    finally:
        await _stop_stack(upstream, gateway, task)


@pytest.mark.asyncio
async def test_ended_and_refused_sessions_leave_no_owner_behind(
    tmp_path: Path,
) -> None:
    """The SDK keeps every DELETEd session (terminated) and every session
    it opened for a request it refused (a GET without an id). Neither may
    keep an owner: clients DELETE on every normal close, so the owner list
    would grow for the life of the process."""
    upstream, gateway, task, app = await _start_stack(tmp_path)
    url = make_gateway_url(gateway)
    try:
        provider = app.state.mcp_gateway_oauth_provider
        admin = await provider.mint_test_token(ADMIN)
        member = await provider.mint_test_token(MEMBER)
        guard = find_guard(app, "/mcp")
        async with httpx.AsyncClient(timeout=30.0) as client:
            ended = await open_session(client, url, admin)
            deleted = await client.delete(url, headers=make_headers(admin, ended))
            assert deleted.status_code == 200, deleted.text
            reused = await send_request(client, url, admin, ended, "tools/list")
            assert reused.status_code == 404, reused.text

            for _ in range(3):
                refused = await client.get(
                    url,
                    headers={**make_headers(admin), "Accept": "text/event-stream"},
                )
                assert refused.status_code == 400, refused.text
                orphan = refused.headers["mcp-session-id"]
                for bearer in (admin, member):
                    used = await send_request(
                        client, url, bearer, orphan, "tools/list",
                    )
                    assert used.status_code == 404, used.text
                session = await open_session(client, url, admin)
                gone = await client.delete(
                    url, headers=make_headers(admin, session),
                )
                assert gone.status_code == 200, gone.text

            last = await open_session(client, url, admin)
            assert set(guard._owners) == {last}
    finally:
        await _stop_stack(upstream, gateway, task)
