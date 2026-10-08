"""How the gateway sign-in state is stored (finding B1, parts c and e).

The whole state (every registered client, access token, refresh token
and client approval) used to be ONE Mongo document, rewritten after
every change. Open registration is anonymous, so a dozen large
registrations (or ~25k ordinary ones) pushed it past Mongo's 16 MB
document limit, and from then on no sign-in, refresh or clean-up could
be saved. Now every item is its own encrypted document, and the old
single document is converted once at startup.

The conversion keeps the old document as it was (second review,
finding 7): deleting it signed every gateway user out on a rollback to
an older build. A record of the conversion stops it from ever running
again, which would bring back every token revoked since.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import structlog
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import MAX_UNUSED_REGISTRATIONS
from mcpolis.adapters.observability.redact_processor import redact_secret_keys
from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.file_oauth_state_repository import (
    FileOAuthStateRepository,
)
from mcpolis.adapters.repositories.mongo_client import (
    COLL_GATEWAY_OAUTH,
    COLL_OAUTH_STATE,
    MotorDatabase,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_oauth_state_repository import (
    MongoOAuthStateRepository,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    StoredAccessToken,
    StoredClient,
    StoredClientApproval,
    StoredRefreshToken,
)
from tests.unit._gateway_oauth_store import (
    make_encryptor,
    make_gateway_provider,
    make_mongo_oauth_state_repository,
)
from tests.unit.mongo_fixture import require_mongo, temp_mongo_database
from tests.unit.test_gateway_oauth_consent import run_google_callback

MEMBER = "member@acme.test"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
DAY = 86400


def make_registration(client_id: str | None = None) -> OAuthClientInformationFull:
    """What an MCP client's dynamic registration stores (Claude Code-like)."""
    return OAuthClientInformationFull(
        client_id=client_id or secrets.token_hex(16),
        client_secret=secrets.token_hex(32),
        client_id_issued_at=int(time.time()),
        client_secret_expires_at=0,
        redirect_uris=[AnyUrl("http://127.0.0.1:33418/callback")],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="Claude Code (mcp-hero)",
    )


def make_stored_client(client_id: str | None = None) -> StoredClient:
    return StoredClient(
        info=make_registration(client_id),
        registered_at=time.time(),
        token_issued=False,
    )


def make_access_token(email: str, client_id: str) -> StoredAccessToken:
    return StoredAccessToken(
        token=secrets.token_urlsafe(32),
        client_id=client_id,
        user_email=email,
        scopes=[],
        expires_at=int(time.time()) + 3600,
    )


def make_refresh_token(email: str, client_id: str) -> StoredRefreshToken:
    return StoredRefreshToken(
        token=secrets.token_urlsafe(32),
        client_id=client_id,
        user_email=email,
        scopes=[],
        created_at=time.time() - 2 * DAY,
    )


def make_approval(email: str, client_id: str) -> StoredClientApproval:
    return StoredClientApproval(
        user_email=email,
        client_id=client_id,
        redirect_identity="https://claude.ai",
        approved_at=time.time() - 2 * DAY,
    )


def make_old_client(client_id: str, registered_days_ago: int) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=secrets.token_hex(32),
        client_id_issued_at=int(time.time()) - registered_days_ago * DAY,
        redirect_uris=[AnyUrl(CLAUDE_REDIRECT)],
        token_endpoint_auth_method="client_secret_post",
        client_name="claudeai",
    )


def old_document_json(
    clients: list[OAuthClientInformationFull],
    access_tokens: list[StoredAccessToken],
    refresh_tokens: list[StoredRefreshToken],
    approvals: list[StoredClientApproval],
) -> dict[str, Any]:
    """The state as builds before per-item storage serialized it
    (``_serialize`` of af6e30c3's ``mongo_oauth_state_repository``)."""
    return {
        "clients": {
            client.client_id: client.model_dump(mode="json") for client in clients
        },
        "access_tokens": {
            s.token: {
                "token": s.token,
                "client_id": s.client_id,
                "user_email": s.user_email,
                "scopes": s.scopes,
                "expires_at": s.expires_at,
            }
            for s in access_tokens
        },
        "refresh_tokens": {
            s.token: {
                "token": s.token,
                "client_id": s.client_id,
                "user_email": s.user_email,
                "scopes": s.scopes,
                "created_at": s.created_at,
            }
            for s in refresh_tokens
        },
        "client_approvals": {
            a.key: {
                "user_email": a.user_email,
                "client_id": a.client_id,
                "redirect_identity": a.redirect_identity,
                "approved_at": a.approved_at,
            }
            for a in approvals
        },
    }


async def write_old_document(
    db: MotorDatabase, encryptor: FieldEncryptor, data: dict[str, Any],
) -> None:
    """Store ``data`` the way builds before per-item storage did (af6e30c3
    ``MongoOAuthStateRepository.save``): ONE document, its JSON encrypted
    by the repository, then again by ``OrgScopedCollection``."""
    coll = OrgScopedCollection(
        db[COLL_OAUTH_STATE], COLL_OAUTH_STATE, encryptor=encryptor,
    )
    await coll.replace_one(
        DEFAULT_ORG_ID,
        {},
        {"encrypted_payload": encryptor.encrypt_string(json.dumps(data))},
        upsert=True,
    )


async def read_old_document(
    db: MotorDatabase, encryptor: FieldEncryptor,
) -> dict[str, Any]:
    """The old document as builds before per-item storage read it
    (af6e30c3 ``MongoOAuthStateRepository.load``): what an older build
    finds after a rollback. Empty when there is none."""
    coll = OrgScopedCollection(
        db[COLL_OAUTH_STATE], COLL_OAUTH_STATE, encryptor=encryptor,
    )
    doc = await coll.find_one(DEFAULT_ORG_ID)
    if doc is None:
        return {}
    data: dict[str, Any] = json.loads(encryptor.decrypt_string(doc["encrypted_payload"]))
    return data


async def count_documents_by_kind(db: MotorDatabase) -> dict[str, int]:
    counts: dict[str, int] = {}
    async for doc in db[COLL_GATEWAY_OAUTH].find({}):
        counts[doc["kind"]] = counts.get(doc["kind"], 0) + 1
    return counts


async def largest_document_bytes(db: MotorDatabase) -> int:
    largest = 0
    async for row in db[COLL_GATEWAY_OAUTH].aggregate([
        {"$group": {"_id": None, "size": {"$max": {"$bsonSize": "$$ROOT"}}}},
    ]):
        largest = int(row["size"])
    return largest


def code_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["code"][0]


class FailingInsertCollection(OrgScopedCollection):
    """Inserts ``inserts_before_failing`` documents, then fails every
    insert — a process that dies half-way through the conversion."""

    def __init__(
        self, db: MotorDatabase, encryptor: FieldEncryptor, inserts_before_failing: int,
    ) -> None:
        super().__init__(db[COLL_GATEWAY_OAUTH], COLL_GATEWAY_OAUTH, encryptor=encryptor)
        self.inserts_left = inserts_before_failing

    async def insert_one(self, org_id: str, doc: dict[str, Any]) -> None:
        if self.inserts_left == 0:
            raise ConnectionError("the database went away")
        self.inserts_left -= 1
        await super().insert_one(org_id, doc)


def make_half_converting_repository(
    db: MotorDatabase, encryptor: FieldEncryptor, inserts_before_failing: int,
) -> MongoOAuthStateRepository:
    return MongoOAuthStateRepository(
        FailingInsertCollection(db, encryptor, inserts_before_failing),
        OrgScopedCollection(db[COLL_OAUTH_STATE], COLL_OAUTH_STATE, encryptor=encryptor),
        encryptor,
    )


# ── Layout: one encrypted document per item ──────────────────────────


async def test_each_item_is_its_own_encrypted_document() -> None:
    """No client secret, token, email or client id reaches the database
    in clear: the payload is encrypted and the lookup key is a keyed
    hash."""
    require_mongo()
    async with temp_mongo_database() as db:
        repo = make_mongo_oauth_state_repository(db, make_encryptor())
        client = make_stored_client("client-1")
        access = make_access_token(MEMBER, "client-1")
        refresh = make_refresh_token(MEMBER, "client-1")
        approval = make_approval(MEMBER, "client-1")
        await repo.apply(OAuthStateChanges(
            clients={"client-1": client},
            access_tokens={access.token: access},
            refresh_tokens={refresh.token: refresh},
            client_approvals={approval.key: approval},
        ))

        raw_docs = await db[COLL_GATEWAY_OAUTH].find({}).to_list(length=None)
        assert sorted(doc["kind"] for doc in raw_docs) == [
            "access_tokens", "client_approvals", "clients", "refresh_tokens",
        ]
        assert all(doc["org_id"] == DEFAULT_ORG_ID for doc in raw_docs)
        assert all(doc["payload"].startswith("enc:v1:") for doc in raw_docs)
        everything_stored = repr(raw_docs)
        assert client.info.client_secret is not None
        for in_clear in (
            access.token, refresh.token, MEMBER, client.info.client_secret,
            "client-1",
        ):
            assert in_clear not in everything_stored

        loaded = await repo.load()
        assert loaded.clients == {"client-1": client}
        assert loaded.access_tokens == {access.token: access}
        assert loaded.refresh_tokens == {refresh.token: refresh}
        assert loaded.client_approvals == {approval.key: approval}


async def test_a_change_replaces_or_deletes_only_its_own_document() -> None:
    require_mongo()
    async with temp_mongo_database() as db:
        repo = make_mongo_oauth_state_repository(db, make_encryptor())
        kept = make_access_token(MEMBER, "c")
        changed = make_access_token(MEMBER, "c")
        deleted = make_access_token(MEMBER, "c")
        await repo.apply(OAuthStateChanges(access_tokens={
            t.token: t for t in (kept, changed, deleted)
        }))
        changed.scopes = ["read"]
        await repo.apply(OAuthStateChanges(access_tokens={
            changed.token: changed, deleted.token: None,
        }))

        assert await count_documents_by_kind(db) == {"access_tokens": 2}
        loaded = await repo.load()
        assert loaded.access_tokens == {kept.token: kept, changed.token: changed}


async def test_gateway_state_survives_25k_anonymous_registrations() -> None:
    """Review B1: ~25k ordinary registrations made the single document
    larger than Mongo's 16 MB limit (it raised ``DocumentTooLarge``).
    Now each stays a small document of its own, and a later sign-in
    is still saved. The backend then keeps only the newest unused
    registrations (``MAX_UNUSED_REGISTRATIONS``), in storage too."""
    require_mongo()
    async with temp_mongo_database() as db:
        repo = make_mongo_oauth_state_repository(db, make_encryptor())
        writers = asyncio.Semaphore(8)

        async def register(client: StoredClient) -> None:
            assert client.info.client_id is not None
            async with writers:
                await repo.apply(OAuthStateChanges(
                    clients={client.info.client_id: client},
                ))

        await asyncio.gather(*(register(make_stored_client()) for _ in range(25_000)))
        assert await largest_document_bytes(db) < 4096

        provider = make_gateway_provider(repo)
        token = await provider.mint_test_token(MEMBER)
        await provider.flush()

        restarted = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
        )
        assert await restarted.verify_token(token) is not None
        kept = MAX_UNUSED_REGISTRATIONS + 1  # with the test-mode client
        assert len(restarted._clients) == kept
        assert await count_documents_by_kind(db) == {
            "clients": kept, "access_tokens": 1, "refresh_tokens": 1,
        }


async def test_a_dozen_one_megabyte_clients_do_not_break_later_sign_ins() -> None:
    """Review B1: about ten anonymous 1 MB registrations filled the
    single document, and every later sign-in failed to save. The caps
    now refuse such registrations; even so, items this large are each
    a document of their own."""
    require_mongo()
    async with temp_mongo_database() as db:
        repo = make_mongo_oauth_state_repository(db, make_encryptor())
        for i in range(20):
            junk = make_registration(f"junk-{i}").model_copy(
                update={"client_name": "x" * 1_000_000},
            )
            await repo.apply(OAuthStateChanges(clients={f"junk-{i}": StoredClient(
                info=junk, registered_at=time.time(), token_issued=False,
            )}))

        token = await make_gateway_provider(repo).mint_test_token(MEMBER)

        restarted = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
        )
        assert await restarted.verify_token(token) is not None, (
            "an ordinary sign-in could not be saved after 20 large registrations"
        )


async def test_a_revoke_is_deleted_from_the_database() -> None:
    require_mongo()
    async with temp_mongo_database() as db:
        provider = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
        )
        token = await provider.mint_test_token(MEMBER)
        assert provider.revoke_user_tokens(MEMBER) == 2
        await provider.flush()

        assert await count_documents_by_kind(db) == {"clients": 1}
        restarted = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
        )
        assert await restarted.verify_token(token) is None


# ── The one-time conversion of the old single document ───────────────


async def test_the_old_document_is_converted_and_every_item_still_works() -> None:
    """Operator decision: production sign-ins survive the deploy."""
    require_mongo()
    async with temp_mongo_database() as db:
        encryptor = make_encryptor()
        signed_in = make_old_client("client-claude", registered_days_ago=40)
        # Old, and holding no token: kept anyway (whether it ever had
        # one is unknown, and forgetting a client in use breaks it).
        idle = make_old_client("client-idle", registered_days_ago=40)
        access = make_access_token(MEMBER, "client-claude")
        refresh = make_refresh_token(MEMBER, "client-claude")
        approval = make_approval(MEMBER, "client-claude")
        await write_old_document(db, encryptor, old_document_json(
            [signed_in, idle], [access], [refresh], [approval],
        ))

        provider = make_gateway_provider(make_mongo_oauth_state_repository(db, encryptor))
        with structlog.testing.capture_logs() as logs:
            await provider.load_state()

        # Kept as it was, for a rollback; the conversion is recorded.
        assert await db[COLL_OAUTH_STATE].count_documents({}) == 1
        assert await count_documents_by_kind(db) == {
            "clients": 2, "access_tokens": 1, "refresh_tokens": 1,
            "client_approvals": 1, "conversion": 1,
        }
        converted = [e for e in logs if e["event"] == "gateway_oauth.single_document.converted"]
        assert converted == [{
            "event": "gateway_oauth.single_document.converted",
            "log_level": "info",
            "clients": 2,
            "access": 1,
            "refresh": 1,
            "client_approvals": 1,
            "already_converted": 0,
        }]
        # The counts survive the log redactor (it masks "token" fields).
        assert redact_secret_keys(None, "info", converted[0]) == converted[0]

        # The access token still lets the member in.
        verified = await provider.verify_token(access.token)
        assert verified is not None and verified.client_id == MEMBER
        # Both clients are still known.
        client = await provider.get_client("client-claude")
        assert client is not None and client.client_secret == signed_in.client_secret
        assert await provider.get_client("client-idle") is not None
        # The refresh token still refreshes.
        loaded_refresh = await provider.load_refresh_token(client, refresh.token)
        assert loaded_refresh is not None
        refreshed = await provider.exchange_refresh_token(client, loaded_refresh, [])
        assert await provider.verify_token(refreshed.access_token) is not None
        # The approval still skips the consent page: Google sign-in goes
        # straight to a code, which exchanges for tokens.
        assert await provider.is_client_approved(MEMBER, "client-claude", CLAUDE_REDIRECT)
        url = await run_google_callback(provider, client, MEMBER, CLAUDE_REDIRECT)
        code = await provider.load_authorization_code(client, code_from(url))
        assert code is not None
        signed_in_again = await provider.exchange_authorization_code(client, code)

        # And all of it survives the next restart, which converts nothing.
        restarted = make_gateway_provider(make_mongo_oauth_state_repository(db, encryptor))
        with structlog.testing.capture_logs() as logs:
            await restarted.load_state()
        assert not [e for e in logs if e["event"] == "gateway_oauth.single_document.converted"]
        for token in (refreshed.access_token, signed_in_again.access_token):
            assert await restarted.verify_token(token) is not None
        assert await restarted.get_client("client-idle") is not None


async def test_a_conversion_cut_half_way_is_finished_by_the_next_start() -> None:
    """The process dies after storing some items: the conversion is not
    recorded, and the next start stores the rest, each item once."""
    require_mongo()
    async with temp_mongo_database() as db:
        encryptor = make_encryptor()
        clients = [make_old_client(f"client-{i}", registered_days_ago=3) for i in range(3)]
        access_tokens = [make_access_token(MEMBER, "client-0") for _ in range(3)]
        refresh_tokens = [make_refresh_token(MEMBER, "client-0") for _ in range(3)]
        approvals = [make_approval(MEMBER, f"client-{i}") for i in range(3)]
        await write_old_document(db, encryptor, old_document_json(
            clients, access_tokens, refresh_tokens, approvals,
        ))

        with pytest.raises(ConnectionError):
            await make_half_converting_repository(db, encryptor, 5).load()
        assert await db[COLL_OAUTH_STATE].count_documents({}) == 1
        assert sum((await count_documents_by_kind(db)).values()) == 5

        with structlog.testing.capture_logs() as logs:
            snapshot = await make_mongo_oauth_state_repository(db, encryptor).load()

        assert await db[COLL_OAUTH_STATE].count_documents({}) == 1
        assert await count_documents_by_kind(db) == {
            "clients": 3, "access_tokens": 3, "refresh_tokens": 3,
            "client_approvals": 3, "conversion": 1,
        }
        assert [e["already_converted"] for e in logs
                if e["event"] == "gateway_oauth.single_document.converted"] == [5]
        assert {t.token for t in access_tokens} == set(snapshot.access_tokens)
        assert {t.token for t in refresh_tokens} == set(snapshot.refresh_tokens)
        assert {a.key for a in approvals} == set(snapshot.client_approvals)
        assert {f"client-{i}" for i in range(3)} == set(snapshot.clients)


async def test_a_converted_item_never_overwrites_a_newer_copy() -> None:
    """A conversion finishing what a cut one started keeps what is
    stored."""
    require_mongo()
    async with temp_mongo_database() as db:
        encryptor = make_encryptor()
        refresh = make_refresh_token(MEMBER, "client-0")
        await write_old_document(db, encryptor, old_document_json([], [], [refresh], []))
        newer = StoredRefreshToken(
            token=refresh.token, client_id="client-0", user_email=MEMBER,
            scopes=["newer"], created_at=refresh.created_at,
        )
        repo = make_mongo_oauth_state_repository(db, encryptor)
        await repo.apply(OAuthStateChanges(refresh_tokens={refresh.token: newer}))

        snapshot = await repo.load()

        assert snapshot.refresh_tokens == {refresh.token: newer}
        assert await count_documents_by_kind(db) == {
            "refresh_tokens": 1, "conversion": 1,
        }


async def test_rolling_back_after_the_conversion_keeps_every_sign_in() -> None:
    """Second review, finding 7 (``make mcpolis-rollback`` after this
    deploy): the conversion deleted the old document, so the older build
    found no gateway state at all, and every registration (claude.ai
    connectors...) and sign-in was gone."""
    require_mongo()
    async with temp_mongo_database() as db:
        encryptor = make_encryptor()
        client = make_old_client("client-claude", registered_days_ago=40)
        access = make_access_token(MEMBER, "client-claude")
        refresh = make_refresh_token(MEMBER, "client-claude")
        before = old_document_json([client], [access], [refresh], [])
        await write_old_document(db, encryptor, before)

        await make_gateway_provider(
            make_mongo_oauth_state_repository(db, encryptor),
        ).load_state()

        assert await read_old_document(db, encryptor) == before


async def test_a_revoked_token_stays_revoked_when_an_old_backend_rewrites_its_document() -> None:
    """The conversion runs once. An older build that still runs (a
    rollback, or two backends at once) saves its document again with
    the tokens it knows; the next start must not convert it again, or
    tokens revoked since the conversion come back."""
    require_mongo()
    async with temp_mongo_database() as db:
        encryptor = make_encryptor()
        client = make_old_client("client-claude", registered_days_ago=40)
        refresh = make_refresh_token(MEMBER, "client-claude")
        old_state = old_document_json([client], [], [refresh], [])
        await write_old_document(db, encryptor, old_state)
        provider = make_gateway_provider(make_mongo_oauth_state_repository(db, encryptor))
        await provider.load_state()
        assert provider.revoke_user_tokens(MEMBER) == 1
        await provider.flush()

        await write_old_document(db, encryptor, old_state)  # the older build saves
        restarted = make_gateway_provider(make_mongo_oauth_state_repository(db, encryptor))
        with structlog.testing.capture_logs() as logs:
            loaded_client = await restarted.get_client("client-claude")

        assert loaded_client is not None
        assert await restarted.load_refresh_token(loaded_client, refresh.token) is None
        assert not [e for e in logs if e["event"] == "gateway_oauth.single_document.converted"]


async def test_two_new_backends_converting_at_once_store_each_item_once() -> None:
    require_mongo()
    async with temp_mongo_database() as db:
        encryptor = make_encryptor()
        clients = [make_old_client(f"c-{i}", registered_days_ago=3) for i in range(20)]
        access_tokens = [make_access_token(MEMBER, "c-0") for _ in range(20)]
        await write_old_document(
            db, encryptor, old_document_json(clients, access_tokens, [], []),
        )

        first, second = await asyncio.gather(
            make_mongo_oauth_state_repository(db, encryptor).load(),
            make_mongo_oauth_state_repository(db, encryptor).load(),
        )

        assert await count_documents_by_kind(db) == {
            "clients": 20, "access_tokens": 20, "conversion": 1,
        }
        tokens = {t.token for t in access_tokens}
        assert set(first.access_tokens) == set(second.access_tokens) == tokens


async def test_a_partly_invalid_old_document_converts_the_rest() -> None:
    """One bad client and one bad token are skipped (and logged), the
    rest converted. The old document keeps them, as it was."""
    require_mongo()
    async with temp_mongo_database() as db:
        encryptor = make_encryptor()
        good = make_access_token(MEMBER, "client-claude")
        data = old_document_json(
            [make_old_client("client-claude", registered_days_ago=3)], [good], [], [],
        )
        data["clients"]["client-bad"] = {
            "client_id": "client-bad", "redirect_uris": "not-a-list",
        }
        data["access_tokens"]["bad-token"] = {"client_id": "client-claude"}
        await write_old_document(db, encryptor, data)

        provider = make_gateway_provider(make_mongo_oauth_state_repository(db, encryptor))
        with structlog.testing.capture_logs() as logs:
            await provider.load_state()

        assert await provider.verify_token(good.token) is not None
        assert await provider.get_client("client-bad") is None
        assert [
            e["kind"] for e in logs if e["event"] == "gateway_oauth.stored_item.invalid"
        ] == ["clients", "access_tokens"]
        assert await read_old_document(db, encryptor) == data


async def test_an_old_document_without_a_payload_stops_the_startup() -> None:
    """Pinned: nothing is converted, nor recorded, so a fixed document is
    converted at the next start (the previous build is untouched)."""
    require_mongo()
    async with temp_mongo_database() as db:
        await db[COLL_OAUTH_STATE].insert_one({"org_id": DEFAULT_ORG_ID})
        provider = make_gateway_provider(
            make_mongo_oauth_state_repository(db, make_encryptor()),
        )

        with pytest.raises(ValueError, match="no payload"):
            await provider.load_state()

        assert await count_documents_by_kind(db) == {}


# ── A stored item that cannot be read ────────────────────────────────


async def test_one_document_that_does_not_decrypt_does_not_block_every_sign_in() -> None:
    """Second review, finding 20: every payload was decrypted at once,
    so one document written under another key (a partial restore, a cut
    write, a hand edit) stopped the whole backend from starting. It is
    now skipped, logged, and left in place."""
    require_mongo()
    async with temp_mongo_database() as db:
        encryptor = make_encryptor()
        good = make_access_token(MEMBER, "c")
        await make_mongo_oauth_state_repository(db, encryptor).apply(
            OAuthStateChanges(access_tokens={good.token: good}),
        )
        other_key = FieldEncryptor.from_master_secret("another-environment")
        await db[COLL_GATEWAY_OAUTH].insert_one({
            "org_id": DEFAULT_ORG_ID,
            "kind": "access_tokens",
            "key": encryptor.lookup_hash("restored-token"),
            "payload": other_key.encrypt_string('{"token": "restored-token"}'),
        })

        provider = make_gateway_provider(make_mongo_oauth_state_repository(db, encryptor))
        with structlog.testing.capture_logs() as logs:
            await provider.load_state()

        assert await provider.verify_token(good.token) is not None
        [unreadable] = [
            e for e in logs if e["event"] == "gateway_oauth.stored_item.unreadable"
        ]
        assert unreadable["kind"] == "access_tokens"
        assert unreadable["error_type"] == "InvalidTag"
        assert await count_documents_by_kind(db) == {"access_tokens": 2}


# ── Standalone: the file repository ──────────────────────────────────


async def test_a_file_written_by_an_older_build_still_loads(tmp_path: Path) -> None:
    """Standalone keeps one JSON file (no size ceiling there); clients
    used to be stored as the bare registration."""
    client = make_old_client("client-claude", registered_days_ago=40)
    access = make_access_token(MEMBER, "client-claude")
    refresh = make_refresh_token(MEMBER, "client-claude")
    approval = make_approval(MEMBER, "client-claude")
    (tmp_path / "oauth_state.json").write_text(json.dumps(
        old_document_json([client], [access], [refresh], [approval]),
    ))

    provider = make_gateway_provider(FileOAuthStateRepository(tmp_path))

    loaded = await provider.get_client("client-claude")
    assert loaded is not None and loaded.client_secret == client.client_secret
    assert await provider.verify_token(access.token) is not None
    assert await provider.load_refresh_token(loaded, refresh.token) is not None
    assert await provider.is_client_approved(MEMBER, "client-claude", CLAUDE_REDIRECT)
