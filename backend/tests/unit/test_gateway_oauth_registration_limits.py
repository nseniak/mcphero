"""Open client registration is bounded (finding B1, parts a and b).

``/register`` answers anyone. Before, it took a body of any size (a
1 MB client name was accepted) and kept every registration forever.
Now the request body, each value and each list are capped, and a
registration that never received a token is forgotten after a day. A
client that did receive one is kept for good: clients like claude.ai
reuse their client id long after their last sign-in.
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    MAX_REGISTRATION_LIST,
    MAX_SIGN_IN_TEXT,
    UNUSED_REGISTRATION_TTL,
)
from mcpolis.adapters.repositories.file_oauth_state_repository import (
    FileOAuthStateRepository,
)
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    StoredClient,
    StoredClientApproval,
)
from mcpolis.entrypoints.middleware.request_body_limit import RequestBodyLimit
from tests.unit._gateway_oauth_store import (
    InMemoryOAuthStateRepository,
    make_gateway_provider,
)
from tests.unit.test_dashboard_api import make_oauth_test_client
from tests.unit.test_gateway_oauth_consent import (
    consent_token_from,
    run_google_callback,
)

MEMBER = "member@acme.test"
REDIRECT = "http://127.0.0.1:33418/callback"


def make_registration_body(**overrides: object) -> dict[str, object]:
    """A ``/register`` request body like Claude Code's."""
    body: dict[str, object] = {
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Claude Code (mcp-hero)",
    }
    body.update(overrides)
    return body


def make_client(client_id: str, **overrides: Any) -> OAuthClientInformationFull:
    fields: dict[str, Any] = {
        "client_id": client_id,
        "redirect_uris": [AnyUrl(REDIRECT)],
        "client_name": "Claude Code (mcp-hero)",
    }
    fields.update(overrides)
    return OAuthClientInformationFull(**fields)


def make_stored_client(client_id: str, *, age_seconds: float, token_issued: bool) -> StoredClient:
    return StoredClient(
        info=make_client(client_id),
        registered_at=time.time() - age_seconds,
        token_issued=token_issued,
    )


def make_approval(email: str, client_id: str) -> StoredClientApproval:
    return StoredClientApproval(
        user_email=email,
        client_id=client_id,
        redirect_identity="http://127.0.0.1",
        approved_at=time.time(),
    )


# ── a. Caps ──────────────────────────────────────────────────────────


def test_anonymous_registration_refuses_a_one_megabyte_client(tmp_path: Path) -> None:
    """Review B1: /mcp/register took a 1 MB client name (HTTP 201), and a
    dozen of those filled the 16 MB state document."""
    client = make_oauth_test_client(tmp_path)

    resp = client.post("/mcp/register", json=make_registration_body(client_name="x" * 1_000_000))

    assert resp.status_code == 413, resp.text
    assert resp.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize("path", ["/mcp/register", "/admin-mcp/register"])
def test_every_sign_in_mount_caps_the_registration_body(tmp_path: Path, path: str) -> None:
    client = make_oauth_test_client(tmp_path)

    too_large = client.post(path, json=make_registration_body(client_name="x" * 20_000))
    normal = client.post(path, json=make_registration_body())

    assert too_large.status_code == 413, too_large.text
    assert normal.status_code == 201, normal.text


def test_an_oversized_registration_value_is_refused(tmp_path: Path) -> None:
    client = make_oauth_test_client(tmp_path)

    resp = client.post(
        "/mcp/register",
        json=make_registration_body(client_name="x" * (MAX_SIGN_IN_TEXT + 1)),
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_client_metadata"
    assert "client_name" in resp.json()["error_description"]


def test_too_many_redirect_uris_are_refused(tmp_path: Path) -> None:
    client = make_oauth_test_client(tmp_path)
    redirects = [
        f"http://127.0.0.1:{4000 + i}/callback" for i in range(MAX_REGISTRATION_LIST + 1)
    ]

    resp = client.post("/mcp/register", json=make_registration_body(redirect_uris=redirects))

    assert resp.status_code == 400, resp.text
    assert "redirect_uris" in resp.json()["error_description"]


async def test_a_body_sent_without_a_length_is_capped_too() -> None:
    """A chunked body declares no length: the cap counts what arrives,
    and answers before the app has read (or parsed) it all."""
    read: list[bytes] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            read.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await JSONResponse({"read": "all"})(scope, receive, send)

    def refusal() -> Response:
        return JSONResponse({"error": "too large"}, status_code=413)

    chunks = [b"x" * 600, b"x" * 600, b"x" * 600]

    async def receive() -> Message:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/register", "headers": []}
    await RequestBodyLimit(app, 1000, refusal)(scope, receive, send)

    assert sent[0]["status"] == 413
    assert len(read) == 1, "the app went on reading past the limit"


async def test_a_one_megabyte_registration_is_refused_by_the_provider() -> None:
    """The cap holds behind every route: the provider itself refuses."""
    provider = make_gateway_provider(InMemoryOAuthStateRepository())

    with pytest.raises(RegistrationError) as refused:
        await provider.register_client(make_client("junk", client_name="x" * 1_000_000))

    assert refused.value.error == "invalid_client_metadata"
    assert await provider.get_client("junk") is None


async def test_registrations_as_large_as_the_caps_allow_are_accepted() -> None:
    provider = make_gateway_provider(InMemoryOAuthStateRepository())
    largest = "x" * MAX_SIGN_IN_TEXT

    await provider.register_client(make_client(
        "large",
        client_name=largest,
        redirect_uris=[
            AnyUrl(f"https://example.test/{j}/{largest[:1900]}")
            for j in range(MAX_REGISTRATION_LIST)
        ],
    ))

    assert await provider.get_client("large") is not None


# ── b. Registrations that never received a token expire ───────────────


async def test_a_registration_that_never_got_a_token_is_forgotten_after_a_day(
    tmp_path: Path,
) -> None:
    await FileOAuthStateRepository(tmp_path).apply(OAuthStateChanges(
        clients={
            "abandoned": make_stored_client(
                "abandoned", age_seconds=UNUSED_REGISTRATION_TTL + 60, token_issued=False,
            ),
            "in-progress": make_stored_client(
                "in-progress", age_seconds=UNUSED_REGISTRATION_TTL - 60, token_issued=False,
            ),
            "signed-in-long-ago": make_stored_client(
                "signed-in-long-ago", age_seconds=40 * 86400, token_issued=True,
            ),
        },
        client_approvals={"approval": make_approval(MEMBER, "abandoned")},
    ))

    provider = make_gateway_provider(FileOAuthStateRepository(tmp_path))

    assert await provider.get_client("abandoned") is None
    assert await provider.get_client("in-progress") is not None
    assert await provider.get_client("signed-in-long-ago") is not None
    await provider.flush()
    stored = await FileOAuthStateRepository(tmp_path).load()
    assert set(stored.clients) == {"in-progress", "signed-in-long-ago"}
    assert stored.client_approvals == {}


async def test_a_client_that_received_a_token_is_kept_for_good() -> None:
    repo = InMemoryOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client("c-1")
    url = await run_google_callback(provider, client, MEMBER, REDIRECT)
    url = await provider.resolve_consent(consent_token_from(url), approve=True)
    assert "code=" in url
    code = await provider.load_authorization_code(
        client, url.split("code=")[1].split("&")[0],
    )
    assert code is not None
    await provider.exchange_authorization_code(client, code)
    assert repo.stored.clients["c-1"].token_issued

    # Forty days on, its tokens long expired, the client comes back.
    repo.stored.clients["c-1"] = replace(
        repo.stored.clients["c-1"], registered_at=time.time() - 40 * 86400,
    )
    restarted = make_gateway_provider(repo)
    assert await restarted.get_client("c-1") is not None


async def test_the_clean_up_also_runs_while_the_backend_runs() -> None:
    repo = InMemoryOAuthStateRepository()
    provider = make_gateway_provider(repo)
    await provider.register_client(make_client("abandoned"))
    provider._clients["abandoned"] = replace(
        provider._clients["abandoned"],
        registered_at=time.time() - UNUSED_REGISTRATION_TTL - 60,
    )
    assert await provider.get_client("abandoned") is None  # refused at once

    provider._next_cleanup_at = 0  # the clean-up is due
    await provider.mint_test_token(MEMBER)
    await provider.flush()

    assert "abandoned" not in provider._clients
    assert "abandoned" not in repo.stored.clients
