"""Sign-in requests can't grow server state without bound (follow-up
to finding B1).

``/register`` and ``/authorize`` answer anyone. Per-address rate limits
don't bound the total across many addresses, so:

    registrations that never received a token:
        at most max_unused_registrations
        (in memory, and deleted from storage)
    pending sign-ins (an /authorize waiting for Google):
        forgotten after PENDING_SIGN_IN_TTL
        at most max_pending_sign_ins
        each kept value at most MAX_SIGN_IN_TEXT characters

A client that received a token never counts, and is never pushed out.

After Google sign-in (members only, but a member's session can be
replayed from many addresses):

    codes not yet exchanged, consent pages not yet answered:
        dropped once expired: when a new one is made, and by the
        periodic clean-up
        at most max_unexchanged_codes / max_pending_consents

Which one goes (second review, finding 8: the caps were one queue for
everyone, so ~67 addresses ended every real sign-in in progress):

    a new item from a source holding max_per_source -> that source's oldest
    all of them at their total cap -> the oldest of the source holding the most
                                      (the new item's own source first of
                                      those holding as many)
    source = the request's address, an IPv6 one per /48 (registrations,
             pending sign-ins), the member (codes, consent pages)
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import structlog
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    AUTH_CODE_TTL,
    CONSENT_TTL,
    MAX_PENDING_SIGN_INS,
    MAX_SIGN_IN_TEXT,
    MAX_UNUSED_REGISTRATIONS,
    PENDING_SIGN_IN_TTL,
    McpGatewayOAuthProvider,
    SignInLimits,
    sign_in_requests_from,
)
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    StoredClient,
)
from mcpolis.entrypoints.middleware.rate_limit_middleware import sign_in_source
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
    run_google_callback,
)

MEMBER = "member@acme.test"
REDIRECT = "http://127.0.0.1:33418/callback"


def make_client(client_id: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl(REDIRECT)],
        client_name="Claude Code (mcp-hero)",
    )


def make_capped_provider(
    repo: InMemoryOAuthStateRepository,
    max_unused_registrations: int = 1_000,
    max_pending_sign_ins: int = 1_000,
    max_unexchanged_codes: int = 1_000,
    max_pending_consents: int = 1_000,
    max_per_source: int = 1_000,
) -> McpGatewayOAuthProvider:
    return make_gateway_provider(repo, limits=SignInLimits(
        max_unused_registrations=max_unused_registrations,
        max_pending_sign_ins=max_pending_sign_ins,
        max_unexchanged_codes=max_unexchanged_codes,
        max_pending_consents=max_pending_consents,
        max_per_source=max_per_source,
    ))


def make_authorize_params(state: str = "client-state") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=None,
        code_challenge="pkce-challenge",
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
    )


async def register_all(
    provider: McpGatewayOAuthProvider, client_ids: list[str],
) -> None:
    for client_id in client_ids:
        await provider.register_client(make_client(client_id))


async def known_clients(
    provider: McpGatewayOAuthProvider, client_ids: list[str],
) -> list[str]:
    return [cid for cid in client_ids if await provider.get_client(cid) is not None]


async def start_sign_ins(provider: McpGatewayOAuthProvider, count: int) -> list[str]:
    """``count`` anonymous /authorize requests; their Google states,
    oldest first."""
    client = make_client("c-1")
    await provider.register_client(client)
    states: list[str] = []
    for _ in range(count):
        await provider.authorize(client, make_authorize_params())
        states.append(next(reversed(provider._pending_auths)))
    return states


async def sign_in_until_code(
    provider: McpGatewayOAuthProvider,
    client: OAuthClientInformationFull,
    email: str,
) -> str:
    """One whole Google sign-in (Google mocked) that stops at the code,
    never exchanged; approves the consent page if one comes up."""
    url = await run_google_callback(provider, client, email, REDIRECT)
    if "/mcp/oauth/consent" in url:
        url = await provider.resolve_consent(consent_token_from(url), approve=True)
    return code_from(url)


async def open_consent_page(
    provider: McpGatewayOAuthProvider, client_id: str, email: str = MEMBER,
) -> str:
    """A Google sign-in for a client not approved yet: its consent token."""
    url = await run_google_callback(provider, make_client(client_id), email, REDIRECT)
    return consent_token_from(url)


async def start_sign_ins_from(
    provider: McpGatewayOAuthProvider, address: str, count: int,
) -> list[str]:
    """``start_sign_ins``, the requests coming from ``address``."""
    with sign_in_requests_from(address):
        return await start_sign_ins(provider, count)


async def register_all_from(
    provider: McpGatewayOAuthProvider, address: str, client_ids: list[str],
) -> None:
    """``register_all``, the requests coming from ``address``."""
    with sign_in_requests_from(address):
        await register_all(provider, client_ids)


def expire_pending_sign_in(provider: McpGatewayOAuthProvider, state: str) -> None:
    pending = provider._pending_auths[state]
    provider._pending_auths[state] = replace(
        pending, created_at=pending.created_at - PENDING_SIGN_IN_TTL - 1,
    )


def expire_code(provider: McpGatewayOAuthProvider, code: str) -> None:
    stored = provider._auth_codes[code]
    provider._auth_codes[code] = replace(
        stored, created_at=stored.created_at - AUTH_CODE_TTL - 1,
    )


def expire_consent_page(provider: McpGatewayOAuthProvider, consent: str) -> None:
    pending = provider._pending_consents[consent]
    provider._pending_consents[consent] = replace(
        pending, created_at=pending.created_at - CONSENT_TTL - 1,
    )


def code_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["code"][0]


# ── Registrations that never received a token ────────────────────────


async def test_unused_registrations_beyond_the_cap_push_out_the_oldest() -> None:
    repo = InMemoryOAuthStateRepository()
    provider = make_capped_provider(repo, max_unused_registrations=3)

    await register_all(provider, ["c-1", "c-2", "c-3", "c-4", "c-5"])

    assert await known_clients(provider, ["c-1", "c-2", "c-3", "c-4", "c-5"]) == [
        "c-3", "c-4", "c-5",
    ]
    await provider.flush()
    assert set(repo.stored.clients) == {"c-3", "c-4", "c-5"}


async def test_the_cap_keeps_storage_bounded_in_mongo() -> None:
    require_mongo()
    async with temp_mongo_database() as db:
        provider = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
            limits=SignInLimits(max_unused_registrations=3),
        )

        await register_all(provider, [f"c-{i}" for i in range(10)])
        await provider.flush()

        assert await db["gateway_oauth"].count_documents({"kind": "clients"}) == 3
        restarted = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
        )
        assert await known_clients(restarted, [f"c-{i}" for i in range(10)]) == [
            "c-7", "c-8", "c-9",
        ]


async def test_a_client_that_received_a_token_never_counts_and_never_goes() -> None:
    repo = InMemoryOAuthStateRepository()
    provider = make_capped_provider(repo, max_unused_registrations=2)
    signed_in = make_client("signed-in")
    url = await run_google_callback(provider, signed_in, MEMBER, REDIRECT)
    url = await provider.resolve_consent(consent_token_from(url), approve=True)
    code = await provider.load_authorization_code(signed_in, code_from(url))
    assert code is not None
    await provider.exchange_authorization_code(signed_in, code)

    await register_all(provider, ["c-1", "c-2", "c-3"])

    assert await known_clients(provider, ["signed-in", "c-1", "c-2", "c-3"]) == [
        "signed-in", "c-2", "c-3",
    ]


async def test_a_pushed_out_registration_takes_its_approvals_along() -> None:
    """Approved, but its code never exchanged: still unused."""
    repo = InMemoryOAuthStateRepository()
    provider = make_capped_provider(repo, max_unused_registrations=1)
    client = make_client("c-1")
    url = await run_google_callback(provider, client, MEMBER, REDIRECT)
    await provider.resolve_consent(consent_token_from(url), approve=True)
    assert await provider.is_client_approved(MEMBER, "c-1", REDIRECT)

    await provider.register_client(make_client("c-2"))

    assert await provider.get_client("c-1") is None
    assert not await provider.is_client_approved(MEMBER, "c-1", REDIRECT)
    await provider.flush()
    assert repo.stored.client_approvals == {}


async def test_the_cap_holds_for_registrations_already_stored() -> None:
    """Storage holding more than the cap (written before it, or with a
    larger one) is cut down at startup, oldest first."""
    repo = InMemoryOAuthStateRepository()
    now = time.time()
    await repo.apply(OAuthStateChanges(clients={
        f"c-{i}": StoredClient(
            info=make_client(f"c-{i}"), registered_at=now - 600 + i, token_issued=False,
        )
        for i in range(5)
    }))
    provider = make_capped_provider(repo, max_unused_registrations=3)

    await provider.load_state()
    await provider.flush()

    assert set(provider._clients) == {"c-2", "c-3", "c-4"}
    assert set(repo.stored.clients) == {"c-2", "c-3", "c-4"}


async def test_a_new_registration_is_never_the_one_pushed_out() -> None:
    """After the clock went back, a new registration looks older than
    the ones before it; it still keeps its place."""
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_unused_registrations=2,
    )
    await register_all(provider, ["c-1", "c-2"])
    for client_id in ("c-1", "c-2"):
        provider._clients[client_id] = replace(
            provider._clients[client_id], registered_at=time.time() + 3600,
        )

    await provider.register_client(make_client("c-3"))

    assert await provider.get_client("c-3") is not None
    assert await known_clients(provider, ["c-1", "c-2", "c-3"]) == ["c-2", "c-3"]


async def test_reaching_a_cap_is_logged_at_most_once_per_interval() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_unused_registrations=1,
    )

    with structlog.testing.capture_logs() as logs:
        await register_all(provider, ["c-1", "c-2", "c-3", "c-4"])

    assert [e for e in logs if e["event"] == "gateway_oauth.cap_reached"] == [{
        "event": "gateway_oauth.cap_reached",
        "log_level": "warning",
        "what": "unused registrations",
        "limit": 1,
        "pushed_out": 1,
    }]


# ── Pending sign-ins (an /authorize waiting for Google) ──────────────


async def test_a_pending_sign_in_expires() -> None:
    provider = make_capped_provider(InMemoryOAuthStateRepository())
    [state] = await start_sign_ins(provider, 1)
    expire_pending_sign_in(provider, state)

    with pytest.raises(ValueError, match="Invalid or expired OAuth state"):
        await finish_google_sign_in(provider, state, MEMBER)
    assert state not in provider._pending_auths


async def test_expired_pending_sign_ins_are_cleared_by_the_next_one() -> None:
    provider = make_capped_provider(InMemoryOAuthStateRepository())
    for state in await start_sign_ins(provider, 3):
        expire_pending_sign_in(provider, state)

    [new_state] = await start_sign_ins(provider, 1)

    assert list(provider._pending_auths) == [new_state]


async def test_pending_sign_ins_beyond_the_cap_push_out_the_oldest() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_pending_sign_ins=3,
    )

    states = await start_sign_ins(provider, 5)

    assert list(provider._pending_auths) == states[2:]
    for state in states[:2]:
        with pytest.raises(ValueError, match="Invalid or expired OAuth state"):
            await finish_google_sign_in(provider, state, MEMBER)
    assert "/mcp/oauth/consent" in await finish_google_sign_in(
        provider, states[-1], MEMBER,
    )


async def test_a_pending_sign_in_still_completes_within_its_window() -> None:
    provider = make_capped_provider(InMemoryOAuthStateRepository())
    client = make_client("c-1")

    url = await run_google_callback(provider, client, MEMBER, REDIRECT)

    assert "/mcp/oauth/consent" in url


async def test_an_oversized_authorize_value_is_refused() -> None:
    provider = make_capped_provider(InMemoryOAuthStateRepository())
    client = make_client("c-1")
    await provider.register_client(client)

    with pytest.raises(AuthorizeError) as refused:
        await provider.authorize(
            client, make_authorize_params(state="s" * (MAX_SIGN_IN_TEXT + 1)),
        )

    assert refused.value.error == "invalid_request"
    assert "state" in (refused.value.error_description or "")
    assert len(provider._pending_auths) == 0


def test_authorize_sends_an_oversized_request_back_to_the_client(tmp_path: Path) -> None:
    """Through the SDK: the client's redirect URI gets
    ``error=invalid_request`` and nothing is kept."""
    client = make_oauth_test_client(tmp_path)
    registered = client.post("/mcp/register", json={
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "client_name": "Claude Code (mcp-hero)",
    }).json()

    resp = client.get("/mcp/authorize", params={
        "client_id": registered["client_id"],
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": "c" * (MAX_SIGN_IN_TEXT + 1),
        "code_challenge_method": "S256",
        "state": "client-state",
    }, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["error"] == ["invalid_request"]
    provider = client.app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined,union-attr]
    assert len(provider._pending_auths) == 0


# ── Codes not yet exchanged, consent pages not yet answered ──────────


async def test_an_unexchanged_code_is_dropped_when_a_new_one_is_made() -> None:
    provider = make_capped_provider(InMemoryOAuthStateRepository())
    client = make_client("c-1")
    old_code = await sign_in_until_code(provider, client, MEMBER)
    expire_code(provider, old_code)

    new_code = await sign_in_until_code(provider, client, MEMBER)

    assert list(provider._auth_codes) == [new_code]


async def test_unexchanged_codes_beyond_the_cap_push_out_the_oldest() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_unexchanged_codes=2,
    )
    client = make_client("c-1")

    codes = [await sign_in_until_code(provider, client, MEMBER) for _ in range(4)]

    assert list(provider._auth_codes) == codes[2:]
    assert await provider.load_authorization_code(client, codes[0]) is None
    newest = await provider.load_authorization_code(client, codes[-1])
    assert newest is not None
    tokens = await provider.exchange_authorization_code(client, newest)
    assert await provider.verify_token(tokens.access_token) is not None


async def test_an_unanswered_consent_page_is_dropped_when_a_new_one_opens() -> None:
    provider = make_capped_provider(InMemoryOAuthStateRepository())
    old_consent = await open_consent_page(provider, "c-1")
    expire_consent_page(provider, old_consent)

    new_consent = await open_consent_page(provider, "c-2")

    assert list(provider._pending_consents) == [new_consent]


async def test_consent_pages_beyond_the_cap_push_out_the_oldest() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_pending_consents=2,
    )

    consents = [await open_consent_page(provider, f"c-{i}") for i in range(3)]

    assert list(provider._pending_consents) == consents[1:]
    with pytest.raises(ValueError, match="Invalid or expired consent token"):
        await provider.resolve_consent(consents[0], approve=True)
    assert "code=" in await provider.resolve_consent(consents[-1], approve=True)


async def test_the_periodic_clean_up_drops_expired_sign_in_leftovers() -> None:
    """Nothing new may come to drop them: the clean-up does."""
    provider = make_capped_provider(InMemoryOAuthStateRepository())
    [state] = await start_sign_ins(provider, 1)
    consent = await open_consent_page(provider, "c-2")
    code = await sign_in_until_code(provider, make_client("c-3"), MEMBER)
    expire_pending_sign_in(provider, state)
    expire_consent_page(provider, consent)
    expire_code(provider, code)

    provider._next_cleanup_at = 0  # the clean-up is due
    await provider.flush()

    assert len(provider._pending_auths) == 0
    assert len(provider._pending_consents) == 0
    assert len(provider._auth_codes) == 0


async def test_reaching_the_code_cap_is_logged() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_unexchanged_codes=1,
    )
    client = make_client("c-1")
    await sign_in_until_code(provider, client, MEMBER)

    with structlog.testing.capture_logs() as logs:
        await sign_in_until_code(provider, client, MEMBER)

    assert [
        (e["what"], e["pushed_out"]) for e in logs
        if e["event"] == "gateway_oauth.cap_reached"
    ] == [("unexchanged codes", 1)]


# ── Per source: a flood pushes out its own first (second review, 8) ──


async def test_anonymous_authorize_requests_from_elsewhere_do_not_end_a_sign_in_at_google() -> None:
    """Review probe, at the production caps: 2,000 anonymous /authorize
    requests ended a real person's sign-in while they were at Google's
    page."""
    provider = make_gateway_provider(InMemoryOAuthStateRepository())
    real = make_client("real-client")
    await provider.register_client(real)
    with sign_in_requests_from("198.51.100.7"):
        await provider.authorize(real, make_authorize_params())
    real_state = next(reversed(provider._pending_auths))

    attacker = make_client("attacker-client")
    await provider.register_client(attacker)
    with sign_in_requests_from("203.0.113.66"):
        for _ in range(MAX_PENDING_SIGN_INS):
            await provider.authorize(attacker, make_authorize_params())

    url = await finish_google_sign_in(provider, real_state, MEMBER)
    assert "/mcp/oauth/consent" in url


async def test_anonymous_registrations_from_elsewhere_do_not_end_a_first_sign_in() -> None:
    """Review probe, at the production caps: 2,000 anonymous
    registrations pushed out the registration of a client in the middle
    of its first sign-in, so its code exchange was refused."""
    provider = make_gateway_provider(InMemoryOAuthStateRepository())
    real = make_client("real-client")
    with sign_in_requests_from("198.51.100.7"):
        code = await sign_in_until_code(provider, real, MEMBER)

    await register_all_from(
        provider, "203.0.113.66",
        [f"junk-{i}" for i in range(MAX_UNUSED_REGISTRATIONS)],
    )

    assert await provider.get_client("real-client") is not None
    loaded = await provider.load_authorization_code(real, code)
    assert loaded is not None
    tokens = await provider.exchange_authorization_code(real, loaded)
    assert await provider.verify_token(tokens.access_token) is not None


async def test_one_address_over_its_cap_pushes_out_its_own_oldest() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_per_source=3,
    )
    [other] = await start_sign_ins_from(provider, "198.51.100.7", 1)

    with structlog.testing.capture_logs() as logs:
        flood = await start_sign_ins_from(provider, "203.0.113.66", 5)

    assert list(provider._pending_auths) == [other, *flood[2:]]
    assert [e for e in logs if e["event"] == "gateway_oauth.cap_reached"] == [{
        "event": "gateway_oauth.cap_reached",
        "log_level": "warning",
        "what": "pending sign-ins from one address",
        "limit": 3,
        "pushed_out": 1,
        "source": "203.0.113.66",
    }]


async def test_a_flood_from_many_addresses_pushes_out_its_own_first() -> None:
    """Many addresses together reach the total: what goes is the oldest
    item of the address holding the most, so the flood thins itself out
    and a person's one sign-in stays (until every address holds one)."""
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_pending_sign_ins=20, max_per_source=5,
    )
    [real] = await start_sign_ins_from(provider, "198.51.100.7", 1)

    for i in range(10):
        await start_sign_ins_from(provider, f"203.0.113.{i}", 5)

    assert len(provider._pending_auths) == 20
    assert real in provider._pending_auths
    held: dict[str, int] = {}
    for pending in provider._pending_auths.values():
        held[pending.source] = held.get(pending.source, 0) + 1
    assert held.pop("198.51.100.7") == 1
    assert len(held) == 10 and max(held.values()) == 2


async def test_registrations_from_many_addresses_push_out_their_own_first() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_unused_registrations=10, max_per_source=4,
    )
    await register_all_from(provider, "198.51.100.7", ["real"])

    for i in range(6):
        await register_all_from(
            provider, f"203.0.113.{i}", [f"junk-{i}-{j}" for j in range(4)],
        )

    assert await provider.get_client("real") is not None
    assert len([c for c in provider._clients.values() if not c.token_issued]) == 10


async def test_one_members_codes_never_push_out_another_members() -> None:
    """Codes and consent pages are counted per member: one member's
    Google session replayed from many addresses only pushes out that
    member's own."""
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_unexchanged_codes=4, max_per_source=2,
    )
    client = make_client("c-1")
    other_code = await sign_in_until_code(provider, client, "other@acme.test")

    flood = [await sign_in_until_code(provider, client, MEMBER) for _ in range(5)]

    assert list(provider._auth_codes) == [other_code, *flood[3:]]
    assert await provider.load_authorization_code(client, other_code) is not None


async def test_a_members_letter_case_does_not_make_a_second_member() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_per_source=2,
    )
    client = make_client("c-1")

    codes = [
        await sign_in_until_code(provider, client, email)
        for email in ("member@acme.test", "Member@Acme.test", "MEMBER@ACME.TEST")
    ]

    assert list(provider._auth_codes) == codes[1:]


async def test_one_members_consent_pages_never_push_out_another_members() -> None:
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_pending_consents=4, max_per_source=2,
    )
    other = await open_consent_page(provider, "c-0", email="other@acme.test")

    flood = [await open_consent_page(provider, f"c-{i}") for i in range(1, 6)]

    assert list(provider._pending_consents) == [other, *flood[3:]]
    assert "code=" in await provider.resolve_consent(other, approve=True)


def make_sign_in_through_the_app(
    client: TestClient, headers: dict[str, str] | None = None,
) -> tuple[McpGatewayOAuthProvider, str]:
    """A registration, then an /authorize waiting for Google, sent through
    the whole app (``RateLimitMiddleware`` included). Returns the
    gateway's sign-in provider, which holds both, and the client id."""
    registered = client.post("/mcp/register", json={
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
    }, headers=headers).json()
    resp = client.get("/mcp/authorize", params={
        "client_id": registered["client_id"],
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": "c" * 43,
        "code_challenge_method": "S256",
        "state": "client-state",
    }, headers=headers, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    provider: McpGatewayOAuthProvider = (
        client.app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined,union-attr]
    )
    return provider, registered["client_id"]


def test_a_sign_in_request_counts_against_the_address_the_rate_limiter_sees(
    tmp_path: Path,
) -> None:
    """Through the app: ``RateLimitMiddleware`` hands the gateway the
    address it keys the request by (the test client's peer here)."""
    provider, client_id = make_sign_in_through_the_app(
        make_oauth_test_client(tmp_path),
    )

    assert provider._registration_sources == {client_id: "testclient"}
    assert [p.source for p in provider._pending_auths.values()] == ["testclient"]


def test_an_ipv6_sign_in_request_counts_against_its_48(tmp_path: Path) -> None:
    """Through the app, behind one proxy: an IPv6 caller's sign-in state
    counts against its /48, not its /64 as the request limits do. Anyone
    can get a /48, which holds 65,536 /64s."""
    provider, client_id = make_sign_in_through_the_app(
        make_oauth_test_client(tmp_path, trusted_proxy_hops=1),
        headers={"X-Forwarded-For": "2001:db8:1234:5678::1"},
    )

    assert provider._registration_sources == {client_id: "2001:db8:1234::/48"}
    assert [p.source for p in provider._pending_auths.values()] == [
        "2001:db8:1234::/48",
    ]


def make_subnets_of_one_48(count: int) -> list[str]:
    """``count`` addresses, each in a /64 of its own, all in one /48 (a
    free tunnel-broker allocation holds 65,536 such /64s)."""
    base = ipaddress.IPv6Network("2001:db8:1234::/48")
    return [
        str(subnet[1])
        for _, subnet in zip(range(count), base.subnets(new_prefix=64), strict=False)
    ]


async def test_one_request_from_each_of_2000_ipv6_subnets_does_not_end_a_real_sign_in() -> None:
    """Third review, R1, at the production caps: one /authorize from each
    of 2,000 /64s of one /48. Counted per /64, no per-address limit was
    hit, every source held one item like the real person's, and the
    total cap pushed out the oldest of them all, the real person's
    pending sign-in. Counted per /48, they are one source, which pushes
    out its own."""
    provider = make_gateway_provider(InMemoryOAuthStateRepository())
    real = make_client("real-client")
    await provider.register_client(real)
    with sign_in_requests_from(sign_in_source("198.51.100.7")):
        await provider.authorize(real, make_authorize_params())
    real_state = next(reversed(provider._pending_auths))

    attacker = make_client("attacker-client")
    await provider.register_client(attacker)
    for address in make_subnets_of_one_48(MAX_PENDING_SIGN_INS):
        with sign_in_requests_from(sign_in_source(address)):
            await provider.authorize(attacker, make_authorize_params())

    url = await finish_google_sign_in(provider, real_state, MEMBER)
    assert "/mcp/oauth/consent" in url


async def test_at_the_total_cap_a_tie_pushes_out_the_adding_sources_own_first() -> None:
    """Two sources hold as many items when the total cap is reached by
    the one adding: it loses its own oldest. The other's older item (an
    office's sign-in in progress) used to go first, being older."""
    provider = make_capped_provider(
        InMemoryOAuthStateRepository(), max_pending_sign_ins=3, max_per_source=2,
    )
    office = await start_sign_ins_from(provider, "198.51.100.7", 2)

    flood = await start_sign_ins_from(provider, "203.0.113.66", 2)

    assert list(provider._pending_auths) == [*office, flood[1]]
