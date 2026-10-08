"""A gateway revoke ends the sign-in, and stays ended (finding S5).

``revoke_user_tokens`` is what an admin's revoke, a member's removal
and an operator's sign-out-everywhere all call. Two ways it fell short:

1. Saves were unordered whole-state snapshots: a save that took its
   snapshot before the revoke and landed after it stored the revoked
   tokens again. Memory stayed right, so it showed only after the next
   restart (every deploy). Now every write stores the CURRENT state of
   its own items, one at a time under one lock.
2. It removed the tokens only: a code minted before the revoke still
   exchanged, an open consent page could still be approved, and the
   remembered approvals skipped the next consent page.
"""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import TokenError

from tests.unit._gateway_oauth_store import (
    ScriptedOAuthStateRepository,
    make_gateway_provider,
)
from tests.unit.test_gateway_oauth_consent import (
    consent_token_from,
    make_client_with,
    run_google_callback,
)

VICTIM = "revoked@acme.test"
OTHER = "other@acme.test"
LOOPBACK_REDIRECT = "http://127.0.0.1:5555/cb"


def code_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["code"][0]


# ── 1. A write that started before the revoke can't undo it ──────────


async def test_a_revoke_survives_a_restart() -> None:
    """Review seam test: another user's sign-in is being written (slow
    connection) when an admin removes a member."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    removed_member_token = await provider.mint_test_token(VICTIM)

    gate = repo.hold_next_write()
    other_sign_in = asyncio.create_task(provider.mint_test_token(OTHER))
    await asyncio.wait_for(gate.reached.wait(), timeout=5)

    assert provider.revoke_user_tokens(VICTIM) == 2
    assert await provider.verify_token(removed_member_token) is None

    gate.release.set()
    await other_sign_in
    # Deploy: shutdown stores what is still on its way, then a fresh
    # process loads the store.
    await provider.flush()
    restarted = make_gateway_provider(repo)
    assert await restarted.verify_token(removed_member_token) is None, (
        "the removed member's gateway token works again after a restart"
    )


async def test_a_revoke_survives_a_restart_when_writes_land_out_of_order() -> None:
    """Review seam test: the revoke's own write would be the fast one."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    await provider.load_state()
    await provider.mint_test_token(VICTIM)

    gate = repo.hold_next_write()
    other_save = asyncio.create_task(provider.mint_test_token(OTHER))
    await asyncio.wait_for(gate.reached.wait(), timeout=5)

    assert provider.revoke_user_tokens(VICTIM) == 2
    await asyncio.sleep(0.01)
    gate.release.set()
    await other_save
    await provider.flush()

    restarted = make_gateway_provider(repo)
    await restarted.load_state()
    assert VICTIM not in restarted.get_connected_users(), (
        "the revoked gateway sign-in is back after a restart"
    )
    assert OTHER in restarted.get_connected_users()


async def test_a_revoke_during_the_members_own_sign_in_write_survives_a_restart() -> None:
    """The member's own new tokens are on their way to storage when the
    revoke removes them: the deletion lands after them, not before."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    await provider.load_state()

    gate = repo.hold_next_write()
    sign_in = asyncio.create_task(provider.mint_test_token(VICTIM))
    await asyncio.wait_for(gate.reached.wait(), timeout=5)

    assert provider.revoke_user_tokens(VICTIM) == 2
    # Give a deletion that does not wait its turn the chance to land
    # first (it would delete nothing, and the sign-in write would then
    # store the tokens again).
    await asyncio.sleep(0.05)
    gate.release.set()
    token = await sign_in
    assert await provider.verify_token(token) is None
    await provider.flush()

    restarted = make_gateway_provider(repo)
    assert await restarted.verify_token(token) is None, (
        "the revoked tokens were stored after their deletion"
    )
    assert VICTIM not in restarted.get_connected_users()


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_background_write_queued_before_a_revoke_stores_the_revoke() -> None:
    """Second review, finding 34 (guard; found by mutating the code so a
    background write takes its changes when it is queued instead of when
    it runs). An approval whose save failed is queued for a retry; the
    person is revoked while the retry waits behind another sign-in's
    write; the retry must store the revoke, not the approval."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    consent = consent_token_from(await run_google_callback(provider, client, VICTIM))

    approval_write = repo.hold_next_write()
    repo.fail_writes = 1
    approving = asyncio.create_task(provider.resolve_consent(consent, approve=True))
    await asyncio.wait_for(approval_write.reached.wait(), timeout=5)
    other_write = repo.hold_next_write()
    other = asyncio.create_task(provider.mint_test_token(OTHER))
    await settle()  # OTHER waits for the write lock
    approval_write.release.set()  # the approval's save fails: retried later
    assert "code=" in await approving
    await asyncio.wait_for(other_write.reached.wait(), timeout=5)
    await settle()  # the retry is queued behind OTHER's write

    provider.revoke_user_tokens(VICTIM)
    other_write.release.set()
    await other
    await settle()
    await provider.flush()

    restarted = make_gateway_provider(repo)
    assert not await restarted.is_client_approved(VICTIM, "c-1", LOOPBACK_REDIRECT)


async def test_two_refreshes_with_one_refresh_token_mint_one_pair() -> None:
    """Second review, finding 34 (guard; found by mutating the code so
    the "still there?" check runs before waiting for the write lock). A
    client, or a thief replaying the token, sends the same refresh token
    twice at once: only one may mint a pair, or one refresh token forks
    into two token families."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    await provider.mint_test_token(VICTIM)
    [refresh] = list(provider._refresh_tokens)
    client = make_client_with(
        "test-mcp-client", redirect_uri="http://localhost/test-callback",
    )
    loaded = await provider.load_refresh_token(client, refresh)
    assert loaded is not None

    first_write = repo.hold_next_write()
    first = asyncio.create_task(provider.exchange_refresh_token(client, loaded, []))
    await asyncio.wait_for(first_write.reached.wait(), timeout=5)
    second = asyncio.create_task(provider.exchange_refresh_token(client, loaded, []))
    await settle()
    first_write.release.set()

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert len([r for r in results if isinstance(r, TokenError)]) == 1, results
    assert len(provider._refresh_tokens) == 1


# ── 2. The revoke also ends what would sign them straight back in ────


async def test_a_code_minted_before_a_revoke_no_longer_exchanges() -> None:
    provider = make_gateway_provider(ScriptedOAuthStateRepository())
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, VICTIM)
    url = await provider.resolve_consent(consent_token_from(url), approve=True)
    code = code_from(url)

    provider.revoke_user_tokens(VICTIM)

    assert await provider.load_authorization_code(client, code) is None


async def test_a_code_loaded_before_a_revoke_is_refused_at_exchange() -> None:
    """The token endpoint loads the code, checks PKCE, then exchanges
    it: a revoke in between must still win."""
    provider = make_gateway_provider(ScriptedOAuthStateRepository())
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, VICTIM)
    url = await provider.resolve_consent(consent_token_from(url), approve=True)
    loaded = await provider.load_authorization_code(client, code_from(url))
    assert loaded is not None

    provider.revoke_user_tokens(VICTIM)

    with pytest.raises(TokenError) as refused:
        await provider.exchange_authorization_code(client, loaded)
    assert refused.value.error == "invalid_grant"
    assert VICTIM not in provider.get_connected_users()


async def test_a_refresh_token_loaded_before_a_revoke_is_refused_at_exchange() -> None:
    provider = make_gateway_provider(ScriptedOAuthStateRepository())
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, VICTIM)
    url = await provider.resolve_consent(consent_token_from(url), approve=True)
    code = await provider.load_authorization_code(client, code_from(url))
    assert code is not None
    tokens = await provider.exchange_authorization_code(client, code)
    assert tokens.refresh_token is not None
    loaded = await provider.load_refresh_token(client, tokens.refresh_token)
    assert loaded is not None

    provider.revoke_user_tokens(VICTIM)

    with pytest.raises(TokenError) as refused:
        await provider.exchange_refresh_token(client, loaded, [])
    assert refused.value.error == "invalid_grant"
    assert VICTIM not in provider.get_connected_users()


async def test_a_consent_page_open_at_a_revoke_can_no_longer_be_approved() -> None:
    provider = make_gateway_provider(ScriptedOAuthStateRepository())
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, VICTIM)
    consent = consent_token_from(url)

    provider.revoke_user_tokens(VICTIM)

    with pytest.raises(ValueError):
        await provider.resolve_consent(consent, approve=True)


async def test_a_revoke_while_the_approval_is_being_saved_mints_no_code() -> None:
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, VICTIM)
    gate = repo.hold_next_write()
    approving = asyncio.create_task(
        provider.resolve_consent(consent_token_from(url), approve=True),
    )
    await asyncio.wait_for(gate.reached.wait(), timeout=5)

    provider.revoke_user_tokens(VICTIM)
    gate.release.set()

    with pytest.raises(ValueError):
        await approving
    assert provider._auth_codes == {}
    await provider.flush()
    assert repo.stored.client_approvals == {}


async def test_a_revoke_forgets_client_approvals() -> None:
    """After a revoke (e.g. an admin cutting off a client the member
    connected), the member's next sign-in asks for consent again."""
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, VICTIM)
    await provider.resolve_consent(consent_token_from(url), approve=True)

    provider.revoke_user_tokens(VICTIM)

    again = await run_google_callback(provider, client, VICTIM)
    assert "/mcp/oauth/consent" in again, again
    await provider.flush()
    assert repo.stored.client_approvals == {}


async def test_a_revoke_leaves_other_people_signed_in() -> None:
    repo = ScriptedOAuthStateRepository()
    provider = make_gateway_provider(repo)
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    for email in (VICTIM, OTHER):
        url = await run_google_callback(provider, client, email)
        await provider.resolve_consent(consent_token_from(url), approve=True)
    other_token = await provider.mint_test_token(OTHER)
    open_consent = consent_token_from(
        await run_google_callback(provider, make_client_with("c-2"), OTHER),
    )

    provider.revoke_user_tokens(VICTIM)
    await provider.flush()

    assert await provider.verify_token(other_token) is not None
    assert await provider.is_client_approved(OTHER, "c-1", LOOPBACK_REDIRECT)
    assert await provider.render_consent(open_consent) is not None
    restarted = make_gateway_provider(repo)
    assert await restarted.verify_token(other_token) is not None


async def test_a_revoke_matches_the_address_whatever_its_letter_case() -> None:
    """Second review, finding 21: the tokens carry Google's spelling of
    the address, and an operator's sign-out passes it as typed. Revoking
    ``Bob@Acme.test`` revoked nothing of ``bob@acme.test``."""
    provider = make_gateway_provider(ScriptedOAuthStateRepository())
    client = make_client_with(redirect_uri=LOOPBACK_REDIRECT)
    url = await run_google_callback(provider, client, "bob@acme.test")
    await provider.resolve_consent(consent_token_from(url), approve=True)
    code_url = await run_google_callback(provider, client, "bob@acme.test")
    open_consent = consent_token_from(
        await run_google_callback(provider, make_client_with("c-2"), "bob@acme.test"),
    )
    token = await provider.mint_test_token("bob@acme.test")

    revoked = provider.revoke_user_tokens("Bob@Acme.test")

    assert revoked == 2
    assert await provider.verify_token(token) is None
    assert not await provider.is_client_approved(
        "bob@acme.test", "c-1", LOOPBACK_REDIRECT,
    )
    assert await provider.load_authorization_code(client, code_from(code_url)) is None
    assert await provider.render_consent(open_consent) is None
