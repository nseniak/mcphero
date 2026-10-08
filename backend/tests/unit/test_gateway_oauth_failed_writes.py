"""A failed write to the gateway sign-in store never loses a sign-in
(finding B1, part d).

When the state was one document past Mongo's size limit, every save
failed: the code exchange answered 500, a token refresh dropped the
client's sign-in (its old refresh token was already taken out of
memory) and the expired-token clean-up answered 500. A failed write must
leave memory and storage agreeing on everything a client relies on:

    a grant (new client, new tokens) is stored before anyone gets it;
        failed -> taken back out of memory, the caller gets an error
    a deletion happens in memory at once;
        failed -> kept as unsaved, retried by the next write, and on a
        timer (1 s, 2 s, 4 s ... at most 60 s) even if no write comes
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from itertools import pairwise
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import McpGatewayOAuthProvider
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    StoredAccessToken,
    StoredRefreshToken,
)
from tests.unit._gateway_oauth_store import (
    ScriptedOAuthStateRepository,
    StoreUnavailable,
    make_gateway_provider,
)
from tests.unit.test_gateway_oauth_consent import (
    consent_token_from,
    make_client_with,
    run_google_callback,
)

MEMBER = "member@acme.test"
LOOPBACK_REDIRECT = "http://127.0.0.1:5555/cb"


def code_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["code"][0]


async def sign_in(
    provider: McpGatewayOAuthProvider,
    client: OAuthClientInformationFull,
    email: str,
) -> OAuthToken:
    """Register, Google sign-in, approve, exchange the code."""
    url = await run_google_callback(provider, client, email)
    if "/mcp/oauth/consent" in url:
        url = await provider.resolve_consent(consent_token_from(url), approve=True)
    code = await provider.load_authorization_code(client, code_from(url))
    assert code is not None
    return await provider.exchange_authorization_code(client, code)


def deletes_a_refresh_token(changes: OAuthStateChanges) -> bool:
    return None in changes.refresh_tokens.values()


# ── Grants: stored before anyone gets them ───────────────────────────


async def test_a_failed_registration_write_registers_nothing() -> None:
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    await provider.load_state()
    repo.fail_writes = 1

    with pytest.raises(StoreUnavailable):
        await provider.register_client(make_client_with("c-1"))

    assert await provider.get_client("c-1") is None


async def test_a_registration_stored_despite_a_lost_answer_is_deleted_again() -> None:
    """The database applied the write but the answer was lost: the
    caller got an error, so the stored copy goes at the next write."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    await provider.load_state()
    repo.lose_answers = 1

    with pytest.raises(StoreUnavailable):
        await provider.register_client(make_client_with("c-1"))
    assert "c-1" in repo.stored.clients

    await provider.flush()
    assert "c-1" not in repo.stored.clients
    assert await provider.get_client("c-1") is None


async def test_a_failed_code_exchange_issues_no_tokens() -> None:
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, MEMBER)
    url = await provider.resolve_consent(consent_token_from(url), approve=True)
    code = await provider.load_authorization_code(client, code_from(url))
    assert code is not None
    repo.lose_answers = 1

    with pytest.raises(StoreUnavailable):
        await provider.exchange_authorization_code(client, code)

    assert MEMBER not in provider.get_connected_users()
    await provider.flush()
    assert repo.stored.access_tokens == {}
    assert repo.stored.refresh_tokens == {}


async def test_a_failed_refresh_keeps_the_current_refresh_token_working() -> None:
    """Review B1: the refresh dropped the old token from memory before
    the save that then failed, so the client's sign-in was gone."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    tokens = await sign_in(provider, client, MEMBER)
    assert tokens.refresh_token is not None
    loaded = await provider.load_refresh_token(client, tokens.refresh_token)
    assert loaded is not None
    repo.fail_writes = 1

    with pytest.raises(StoreUnavailable):
        await provider.exchange_refresh_token(client, loaded, [])

    # A restart now still knows the refresh token the client holds...
    restarted = make_gateway_provider(repo)
    assert await restarted.load_refresh_token(client, tokens.refresh_token) is not None
    # ...and so does the running backend: the client's retry works.
    retry = await provider.load_refresh_token(client, tokens.refresh_token)
    assert retry is not None
    refreshed = await provider.exchange_refresh_token(client, retry, [])
    assert await provider.verify_token(refreshed.access_token) is not None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_a_refresh_succeeds_when_only_retiring_the_old_token_fails() -> None:
    """The new pair is stored, so the client gets it; the old token's
    deletion is retried by a later write."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    tokens = await sign_in(provider, client, MEMBER)
    assert tokens.refresh_token is not None
    loaded = await provider.load_refresh_token(client, tokens.refresh_token)
    assert loaded is not None
    repo.refuse = deletes_a_refresh_token

    refreshed = await provider.exchange_refresh_token(client, loaded, [])

    assert refreshed.refresh_token is not None
    restarted = make_gateway_provider(repo)
    assert await restarted.verify_token(refreshed.access_token) is not None
    assert await restarted.load_refresh_token(client, refreshed.refresh_token) is not None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None

    repo.refuse = lambda _changes: False
    await provider.flush()
    assert tokens.refresh_token not in repo.stored.refresh_tokens


async def test_an_approval_that_fails_to_save_does_not_stop_the_sign_in() -> None:
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, MEMBER)
    repo.fail_writes = 1

    url = await provider.resolve_consent(consent_token_from(url), approve=True)

    assert "code=" in url
    await provider.flush()
    assert await make_gateway_provider(repo).is_client_approved(
        MEMBER, "c-1", LOOPBACK_REDIRECT,
    )


# ── Deletions: in memory at once, in storage once it is back ─────────


async def test_an_expired_token_is_refused_even_when_its_deletion_fails() -> None:
    """Review B1: the expired-token clean-up answered 500."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    await provider.load_state()
    expired = StoredAccessToken(
        token="expired", client_id="c-1", user_email=MEMBER, scopes=[],
        expires_at=1,
    )
    old_refresh = StoredRefreshToken(
        token="old-refresh", client_id="c-1", user_email=MEMBER, scopes=[],
        created_at=1.0,
    )
    provider._access_tokens[expired.token] = expired
    provider._refresh_tokens[old_refresh.token] = old_refresh
    repo.fail_writes = 100

    assert await provider.load_access_token("expired") is None
    assert await provider.load_refresh_token(
        make_client_with("c-1"), "old-refresh",
    ) is None
    await asyncio.sleep(0.05)  # the background deletion fails, quietly


async def test_a_revoke_reaches_storage_once_it_is_back() -> None:
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    token = await provider.mint_test_token(MEMBER)
    repo.fail_writes = 100

    assert provider.revoke_user_tokens(MEMBER) == 2
    await asyncio.sleep(0.05)  # the background deletion fails
    assert await provider.verify_token(token) is None
    assert token in repo.stored.access_tokens

    repo.fail_writes = 0
    await provider.flush()
    assert await make_gateway_provider(repo).verify_token(token) is None


async def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Let the event loop run until ``condition()`` holds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_a_failed_revoke_deletion_is_retried_without_other_traffic() -> None:
    """Second review, finding 19: a deletion that failed was retried only
    by later sign-in traffic (or the shutdown flush). On a quiet gateway,
    a crash then brought the revoked token back."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo, write_retry_delay=0.01)
    token = await provider.mint_test_token(MEMBER)
    repo.fail_writes = 1  # the store hiccups exactly when the admin revokes

    assert provider.revoke_user_tokens(MEMBER) == 2
    await wait_until(lambda: token not in repo.stored.access_tokens)

    crashed_and_restarted = make_gateway_provider(repo)  # no flush
    assert await crashed_and_restarted.verify_token(token) is None


async def test_a_failed_rotation_deletion_is_retried_without_other_traffic() -> None:
    """The refresh token a client just rotated away stays usable after a
    crash: a replayed (stolen) old refresh token would mint a second
    family."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo, write_retry_delay=0.01)
    await provider.mint_test_token(MEMBER)
    [old_refresh] = list(provider._refresh_tokens)
    client = make_client_with(
        "test-mcp-client", redirect_uri="http://localhost/test-callback",
    )
    loaded = await provider.load_refresh_token(client, old_refresh)
    assert loaded is not None
    repo.refuse = deletes_a_refresh_token

    await provider.exchange_refresh_token(client, loaded, [])
    await asyncio.sleep(0.1)  # the retries fail too, meanwhile
    assert old_refresh in repo.stored.refresh_tokens
    repo.refuse = lambda _changes: False
    await wait_until(lambda: old_refresh not in repo.stored.refresh_tokens)

    crashed_and_restarted = make_gateway_provider(repo)
    assert await crashed_and_restarted.load_refresh_token(client, old_refresh) is None


async def test_each_failed_retry_waits_twice_as_long() -> None:
    """A store that stays down is not hammered: 1, 2, 4 ... times the
    first wait, at most a minute."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo, write_retry_delay=0.02)
    await provider.mint_test_token(MEMBER)
    attempts: list[float] = []

    def refuse_and_count(_changes: OAuthStateChanges) -> bool:
        attempts.append(asyncio.get_running_loop().time())
        return True

    repo.refuse = refuse_and_count
    provider.revoke_user_tokens(MEMBER)
    await wait_until(lambda: len(attempts) >= 5)

    gaps = [later - earlier for earlier, later in pairwise(attempts)]
    assert all(gap >= 0.02 * 2 ** i * 0.95 for i, gap in enumerate(gaps)), gaps


async def test_one_item_that_cannot_be_written_does_not_block_other_sign_ins() -> None:
    """Review B1: once one save failed for good, every later save failed
    with it. Each write now carries only its own items."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    await provider.load_state()
    repo.refuse = lambda changes: "poison" in changes.clients
    with pytest.raises(StoreUnavailable):
        await provider.register_client(make_client_with("poison"))

    token = await provider.mint_test_token(MEMBER)
    await provider.register_client(make_client_with("c-2"))

    restarted = make_gateway_provider(repo)
    assert await restarted.verify_token(token) is not None
    assert await restarted.get_client("c-2") is not None
    assert await restarted.get_client("poison") is None
