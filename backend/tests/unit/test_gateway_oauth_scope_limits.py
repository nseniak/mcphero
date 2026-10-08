"""A sign-in's scopes are bounded (second review, blocker 1).

``/authorize`` answers anyone. The MCP SDK only checks that each scope a
sign-in asks for is one the client registered, so ``ab ab ab ...``
repeated a million times passed for a client registered with ``ab``:
one anonymous request kept about 17 MB for 10 minutes, the list was
copied into the code and the tokens (stored, and loaded at every boot),
and every refresh could ask for it again. The SDK's check also compares
every scope asked for with every registered one, on the event loop.

    a sign-in request (body, or query string)  > 16 KB -> HTTP 413, unparsed
    a registration's scope                     > 10 names or 2,048 chars -> refused
    an /authorize scope                        > 10 names or 2,048 chars -> refused
    what a sign-in, a code or a token keeps    each scope once, at most 10
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.testclient import TestClient

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    MAX_REGISTRATION_LIST,
    MAX_SIGN_IN_REQUEST_BYTES,
    MAX_SIGN_IN_TEXT,
    McpGatewayOAuthProvider,
)
from mcpolis.adapters.repositories.mongo_client import COLL_GATEWAY_OAUTH, MotorDatabase
from tests.unit._gateway_oauth_store import (
    InMemoryOAuthStateRepository,
    make_encryptor,
    make_gateway_provider,
    make_mongo_oauth_state_repository,
)
from tests.unit.mongo_fixture import require_mongo, temp_mongo_database
from tests.unit.test_dashboard_api import make_oauth_test_client
from tests.unit.test_gateway_oauth_consent import (
    consent_token_from,
    finish_google_sign_in,
)

MEMBER = "self-signup@anyone.test"
REDIRECT = "http://127.0.0.1:33418/callback"
ONE_MEGABYTE = 1_000_000


def make_client(scope: str | None = "ab") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="c-1", redirect_uris=[AnyUrl(REDIRECT)], scope=scope,
    )


def make_params(scopes: list[str] | None) -> AuthorizationParams:
    return AuthorizationParams(
        state="s",
        scopes=scopes,
        code_challenge="c" * 43,
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
    )


def register_over_http(client: TestClient, scope: str | None = "ab") -> str:
    body: dict[str, object] = {
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "client_name": "anonymous",
    }
    if scope is not None:
        body["scope"] = scope
    registered = client.post("/mcp/register", json=body)
    assert registered.status_code == 201, registered.text
    client_id: str = registered.json()["client_id"]
    return client_id


def make_authorize_form(client_id: str, scope: str) -> dict[str, str]:
    return {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": "c" * 43,
        "code_challenge_method": "S256",
        "state": "client-state",
        "scope": scope,
    }


def pending_sign_ins(client: TestClient) -> int:
    provider: McpGatewayOAuthProvider = client.app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined,union-attr]
    return len(provider._pending_auths)


async def sign_in_with_scopes(
    provider: McpGatewayOAuthProvider,
    client: OAuthClientInformationFull,
    scopes: list[str],
) -> str:
    """Register, /authorize asking for ``scopes``, Google sign-in,
    approve: the code."""
    await provider.register_client(client)
    await provider.authorize(client, make_params(scopes))
    url = await finish_google_sign_in(
        provider, next(reversed(provider._pending_auths)), MEMBER,
    )
    url = await provider.resolve_consent(consent_token_from(url), approve=True)
    return parse_qs(urlparse(url).query)["code"][0]


async def largest_stored_document(db: MotorDatabase) -> int:
    largest = 0
    async for row in db[COLL_GATEWAY_OAUTH].aggregate([
        {"$project": {"size": {"$bsonSize": "$$ROOT"}}},
    ]):
        largest = max(largest, int(row["size"]))
    return largest


# ── The request: capped before the SDK parses it ─────────────────────


def test_post_authorize_with_a_one_megabyte_scope_is_refused_unparsed(
    tmp_path: Path,
) -> None:
    """Review probe: this request was accepted, and its 333,333 scopes
    kept with the pending sign-in (17 MB)."""
    client = make_oauth_test_client(tmp_path)
    client_id = register_over_http(client)
    huge_scope = " ".join(["ab"] * (ONE_MEGABYTE // 3))

    resp = client.post(
        "/mcp/authorize",
        data=make_authorize_form(client_id, huge_scope),
        follow_redirects=False,
    )

    assert resp.status_code == 413, resp.text
    assert resp.json()["error"] == "invalid_request"
    assert pending_sign_ins(client) == 0


def test_get_authorize_with_an_oversized_query_is_refused_unparsed(
    tmp_path: Path,
) -> None:
    client = make_oauth_test_client(tmp_path)
    client_id = register_over_http(client)
    query = urlencode(make_authorize_form(
        client_id, " ".join(["ab"] * (MAX_SIGN_IN_REQUEST_BYTES // 3 + 1)),
    ))

    resp = client.get(f"/mcp/authorize?{query}", follow_redirects=False)

    assert resp.status_code == 413, resp.text
    assert pending_sign_ins(client) == 0


def test_a_token_request_over_the_cap_is_refused_unparsed(tmp_path: Path) -> None:
    client = make_oauth_test_client(tmp_path)
    client_id = register_over_http(client)

    resp = client.post("/mcp/token", data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": "r",
        "scope": " ".join(["ab"] * (MAX_SIGN_IN_REQUEST_BYTES // 3 + 1)),
    })

    assert resp.status_code == 413, resp.text
    assert resp.json()["error"] == "invalid_request"


def test_an_authorize_scope_over_the_cap_goes_back_to_the_client(
    tmp_path: Path,
) -> None:
    """Within the request cap, but more than the provider keeps: the SDK
    sends the client back to its redirect URI, and nothing is kept."""
    client = make_oauth_test_client(tmp_path)
    client_id = register_over_http(client)
    scope = " ".join(["ab"] * (MAX_SIGN_IN_TEXT // 3 + 1))

    resp = client.post(
        "/mcp/authorize",
        data=make_authorize_form(client_id, scope),
        follow_redirects=False,
    )

    assert resp.status_code == 302, resp.text
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query.get("error") == ["invalid_request"], resp.headers["location"]
    assert "scope" in query["error_description"][0]
    assert pending_sign_ins(client) == 0


def test_the_largest_authorize_accepted_costs_little_time(tmp_path: Path) -> None:
    """Review probe: the SDK compares each scope asked for with each
    registered one (~1.4e8 comparisons, 0.5 s on the event loop, for one
    anonymous request). Now a registration names at most 10 scopes and a
    request is at most 16 KB: a few thousand comparisons."""
    client = make_oauth_test_client(tmp_path)
    registered = [f"s{i}" for i in range(MAX_REGISTRATION_LIST)]
    client_id = register_over_http(client, " ".join(registered))
    # As many names as the request holds, each the last one registered:
    # the most comparisons a request can cost.
    last = registered[-1]
    fixed = len(urlencode(make_authorize_form(client_id, "")))
    repeats = (MAX_SIGN_IN_REQUEST_BYTES - fixed) // (len(last) + 1)
    form = make_authorize_form(client_id, " ".join([last] * repeats))
    assert len(urlencode(form)) <= MAX_SIGN_IN_REQUEST_BYTES

    started = time.perf_counter()
    resp = client.post("/mcp/authorize", data=form, follow_redirects=False)
    elapsed = time.perf_counter() - started

    assert resp.status_code == 302, resp.text
    assert parse_qs(urlparse(resp.headers["location"]).query).get("error") == [
        "invalid_request",
    ]
    assert elapsed < 0.5, f"one anonymous /authorize took {elapsed:.2f}s"


# ── The registration: at most 10 scopes ──────────────────────────────


def test_a_registration_with_more_than_ten_scopes_is_refused(tmp_path: Path) -> None:
    client = make_oauth_test_client(tmp_path)

    resp = client.post("/mcp/register", json={
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(f"s{i}" for i in range(MAX_REGISTRATION_LIST + 1)),
    })

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_client_metadata"
    assert "scope" in resp.json()["error_description"]


async def test_a_registration_with_ten_scopes_is_accepted() -> None:
    provider = make_gateway_provider(InMemoryOAuthStateRepository())

    await provider.register_client(make_client(
        " ".join(f"s{i}" for i in range(MAX_REGISTRATION_LIST)),
    ))

    assert await provider.get_client("c-1") is not None


# ── What a sign-in keeps ─────────────────────────────────────────────


@pytest.mark.parametrize("count", [MAX_REGISTRATION_LIST + 1, 333_333])
async def test_more_than_ten_scopes_are_refused_at_authorize(count: int) -> None:
    provider = make_gateway_provider(InMemoryOAuthStateRepository())
    client = make_client()
    await provider.register_client(client)

    with pytest.raises(AuthorizeError) as refused:
        await provider.authorize(client, make_params(["ab"] * count))

    assert refused.value.error == "invalid_request"
    assert "scope" in (refused.value.error_description or "")
    assert len(provider._pending_auths) == 0


async def test_a_sign_in_keeps_each_scope_once() -> None:
    provider = make_gateway_provider(InMemoryOAuthStateRepository())
    client = make_client("ab cd")

    code = await sign_in_with_scopes(provider, client, ["ab", "cd", "ab", "ab"])
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    tokens = await provider.exchange_authorization_code(client, loaded)

    assert loaded.scopes == ["ab", "cd"]
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None and access.scopes == ["ab", "cd"]


async def test_a_refresh_cannot_inflate_a_one_scope_token() -> None:
    """Review probe, through ``/token``: a refresh may name scopes, each
    of which must be in the refresh token (repeats allowed by the SDK).
    A token holding ``["ab"]`` refreshed into a pair holding 333,332."""
    provider = make_gateway_provider(InMemoryOAuthStateRepository())
    client = make_client()
    code = await sign_in_with_scopes(provider, client, ["ab"])
    loaded_code = await provider.load_authorization_code(client, code)
    assert loaded_code is not None
    tokens = await provider.exchange_authorization_code(client, loaded_code)
    assert tokens.refresh_token is not None
    loaded = await provider.load_refresh_token(client, tokens.refresh_token)
    assert loaded is not None and loaded.scopes == ["ab"]
    # What the SDK's token handler passes for scope="ab ab ab ...".
    requested = ["ab"] * 333_332

    refreshed = await provider.exchange_refresh_token(client, loaded, requested)

    access = await provider.load_access_token(refreshed.access_token)
    assert access is not None and access.scopes == ["ab"]
    assert refreshed.refresh_token is not None
    refresh = await provider.load_refresh_token(client, refreshed.refresh_token)
    assert refresh is not None and refresh.scopes == ["ab"]


async def test_a_members_sign_in_and_refreshes_store_small_documents() -> None:
    """Review probe: one sign-in and three refreshes stored 13 MB (the
    largest document 2.7 MB), and every boot loaded 1.7 million scope
    names. The sign-in asking for a megabyte of scopes is now refused;
    the refreshes asking for them keep one."""
    require_mongo()
    async with temp_mongo_database() as db:
        provider = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
        )
        client = make_client()
        await provider.register_client(client)
        with pytest.raises(AuthorizeError):
            await provider.authorize(client, make_params(["ab"] * 333_333))

        code = await sign_in_with_scopes(provider, client, ["ab"])
        loaded_code = await provider.load_authorization_code(client, code)
        assert loaded_code is not None
        tokens = await provider.exchange_authorization_code(client, loaded_code)
        for _ in range(3):
            assert tokens.refresh_token is not None
            loaded = await provider.load_refresh_token(client, tokens.refresh_token)
            assert loaded is not None
            tokens = await provider.exchange_refresh_token(
                client, loaded, ["ab"] * 333_332,
            )
        await provider.flush()

        assert await largest_stored_document(db) < 4096
        restarted = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
        )
        await restarted.load_state()
        held = [t.scopes for t in restarted._access_tokens.values()]
        held += [t.scopes for t in restarted._refresh_tokens.values()]
        assert held and all(scopes == ["ab"] for scopes in held)
