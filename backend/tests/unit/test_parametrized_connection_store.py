"""Parameterized ``ConnectionRepository`` tests.

Each test runs against both backends — the file-backed store and the
Mongo-backed store — to prove that swapping the storage adapter does
not change observable behavior. When Mongo is not reachable the
``mongo`` parameter is skipped automatically (see ``mongo_fixture``).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
    OAuthToken,
)
from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONNECTIONS,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_connection_repository import (
    MongoConnectionRepository,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


BACKENDS: list[str] = ["file"] + (["mongo"] if mongo_available() else [])


@asynccontextmanager
async def _make_store(backend: str, tmp_path: Path) -> AsyncIterator[ConnectionStore]:
    if backend == "file":
        yield FileConnectionStore(tmp_path)
        return
    async with temp_mongo_database() as db:
        encryptor = FieldEncryptor.from_master_secret("unit-test-key")
        coll = OrgScopedCollection(
            db[COLL_CONNECTIONS], COLL_CONNECTIONS, encryptor=encryptor,
        )
        yield MongoConnectionRepository(coll)


def _token(
    access: str = "access-123",
    refresh: str | None = "refresh-456",
) -> OAuthToken:
    return OAuthToken(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime(2026, 6, 1, tzinfo=UTC),
        scopes=["read", "write"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_admin_token_roundtrip(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        assert await store.get_admin_token(DEFAULT_ORG_ID, "github") is None
        await store.put_admin_token(
            DEFAULT_ORG_ID, "github", _token(), authorized_by="admin@co.com",
        )
        result = await store.get_admin_token(DEFAULT_ORG_ID, "github")
        assert result is not None
        assert result.access_token == "access-123"
        assert result.refresh_token == "refresh-456"
        assert result.scopes == ["read", "write"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_user_token_roundtrip(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        assert await store.get_user_token(
            DEFAULT_ORG_ID, "alice@co.com", "slack"
        ) is None
        await store.put_user_token(
            DEFAULT_ORG_ID, "alice@co.com", "slack", _token("user-token"),
        )
        result = await store.get_user_token(
            DEFAULT_ORG_ID, "alice@co.com", "slack"
        )
        assert result is not None
        assert result.access_token == "user-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_separate_users_isolated(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        await store.put_user_token(
            DEFAULT_ORG_ID, "alice@co.com", "github", _token("alice-token"),
        )
        await store.put_user_token(
            DEFAULT_ORG_ID, "bob@co.com", "github", _token("bob-token"),
        )
        a = await store.get_user_token(DEFAULT_ORG_ID, "alice@co.com", "github")
        b = await store.get_user_token(DEFAULT_ORG_ID, "bob@co.com", "github")
        assert a is not None and a.access_token == "alice-token"
        assert b is not None and b.access_token == "bob-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_delete_user_token(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        await store.put_user_token(
            DEFAULT_ORG_ID, "bob@co.com", "github", _token(),
        )
        assert await store.get_user_token(
            DEFAULT_ORG_ID, "bob@co.com", "github"
        ) is not None
        await store.delete_user_token(DEFAULT_ORG_ID, "bob@co.com", "github")
        assert await store.get_user_token(
            DEFAULT_ORG_ID, "bob@co.com", "github"
        ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_enabled_default_when_no_marker(
    backend: str, tmp_path: Path,
) -> None:
    """Bistate (Phase E): absence of any marker means enabled. The
    only stored state is an explicit-disabled marker."""
    async with _make_store(backend, tmp_path) as store:
        assert await store.is_enabled(DEFAULT_ORG_ID, "slack") is True
        # set_enabled is idempotent on an already-enabled upstream.
        await store.set_enabled(DEFAULT_ORG_ID, "slack")
        assert await store.is_enabled(DEFAULT_ORG_ID, "slack") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_set_disabled_survives_round_trip(
    backend: str, tmp_path: Path,
) -> None:
    """``set_disabled`` writes an explicit ``enabled: False`` so the
    boot reconciler picks the upstream up via ``get_disabled_ids``
    and skips it across restarts. ``set_enabled`` removes that
    marker — under the bistate semantic the two verbs are inverses."""
    async with _make_store(backend, tmp_path) as store:
        await store.set_disabled(DEFAULT_ORG_ID, "slack")
        assert await store.is_enabled(DEFAULT_ORG_ID, "slack") is False
        assert "slack" in await store.get_disabled_ids(DEFAULT_ORG_ID)
        # set_enabled flips it back by deleting the marker.
        await store.set_enabled(DEFAULT_ORG_ID, "slack")
        assert await store.is_enabled(DEFAULT_ORG_ID, "slack") is True
        assert "slack" not in await store.get_disabled_ids(DEFAULT_ORG_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_set_enabled_idempotent_on_unmarked(
    backend: str, tmp_path: Path,
) -> None:
    """``set_enabled`` on an upstream with no marker is a no-op —
    nothing to delete. Pinned because the bistate rename collapsed
    two prior verbs into this one and the no-marker case is the
    common one (every fresh upstream)."""
    async with _make_store(backend, tmp_path) as store:
        await store.set_enabled(DEFAULT_ORG_ID, "fresh-ups")
        assert await store.is_enabled(DEFAULT_ORG_ID, "fresh-ups") is True
        assert "fresh-ups" not in await store.get_disabled_ids(DEFAULT_ORG_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_pending_code(backend: str, tmp_path: Path) -> None:
    async with _make_store(backend, tmp_path) as store:
        await store.put_pending_code(
            DEFAULT_ORG_ID, "slack", "alice@co.com", "code-1", "state-1",
        )
        popped = await store.pop_pending_code(
            DEFAULT_ORG_ID, "slack", "alice@co.com",
        )
        assert popped == ("code-1", "state-1")
        # Second pop returns None
        assert await store.pop_pending_code(
            DEFAULT_ORG_ID, "slack", "alice@co.com",
        ) is None


# ── oauth_metadata: round-trip across backends (§3.8 / §5.4) ─────
#
# Persists the SDK's discovered ``OAuthMetadata`` so a fresh process
# can pre-populate ``OAuthContext.oauth_metadata`` and the periodic
# refresh branch resolves the upstream's real ``token_endpoint``. The
# load-bearing detail is that the persisted blob round-trips unchanged
# — a Mongo/file divergence here would silently regress §3.8 on cloud
# while leaving the standalone tests green.


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_oauth_metadata_roundtrip(
    backend: str, tmp_path: Path,
) -> None:
    metadata = {
        "issuer": "https://oauth.example.invalid/",
        "authorization_endpoint": "https://oauth.example.invalid/authorize",
        "token_endpoint": "https://oauth.example.invalid/oauth/token",
        "registration_endpoint": "https://oauth.example.invalid/register",
        "scopes_supported": ["read", "write"],
        "response_types_supported": ["code"],
    }
    async with _make_store(backend, tmp_path) as store:
        assert await store.get_oauth_metadata(
            DEFAULT_ORG_ID, "mixpanel", "alice@co.com",
        ) is None
        await store.put_oauth_metadata(
            DEFAULT_ORG_ID, "mixpanel", "alice@co.com", metadata,
        )
        result = await store.get_oauth_metadata(
            DEFAULT_ORG_ID, "mixpanel", "alice@co.com",
        )
        assert result is not None
        assert (
            result["token_endpoint"]
            == "https://oauth.example.invalid/oauth/token"
        )
        assert result["scopes_supported"] == ["read", "write"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_oauth_metadata_isolated_per_user(
    backend: str, tmp_path: Path,
) -> None:
    """Per-user OAuth upstreams (Notion-style) keep one metadata row
    per (upstream, user). One user's discovery must not overwrite
    another's — pin that across both backends."""
    alice_md = {
        "issuer": "https://alice.example.invalid",
        "authorization_endpoint": "https://alice.example.invalid/authorize",
        "token_endpoint": "https://alice.example.invalid/oauth/token",
    }
    bob_md = {
        "issuer": "https://bob.example.invalid",
        "authorization_endpoint": "https://bob.example.invalid/authorize",
        "token_endpoint": "https://bob.example.invalid/oauth/token",
    }
    async with _make_store(backend, tmp_path) as store:
        await store.put_oauth_metadata(
            DEFAULT_ORG_ID, "notion", "alice@co.com", alice_md,
        )
        await store.put_oauth_metadata(
            DEFAULT_ORG_ID, "notion", "bob@co.com", bob_md,
        )
        a = await store.get_oauth_metadata(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        b = await store.get_oauth_metadata(
            DEFAULT_ORG_ID, "notion", "bob@co.com",
        )
        assert a is not None
        assert b is not None
        assert a["issuer"] == "https://alice.example.invalid"
        assert b["issuer"] == "https://bob.example.invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_oauth_metadata_delete(
    backend: str, tmp_path: Path,
) -> None:
    metadata = {
        "issuer": "https://oauth.example.invalid",
        "authorization_endpoint": "https://oauth.example.invalid/authorize",
        "token_endpoint": "https://oauth.example.invalid/oauth/token",
    }
    async with _make_store(backend, tmp_path) as store:
        await store.put_oauth_metadata(
            DEFAULT_ORG_ID, "mixpanel", "alice@co.com", metadata,
        )
        await store.delete_oauth_metadata(
            DEFAULT_ORG_ID, "mixpanel", "alice@co.com",
        )
        assert await store.get_oauth_metadata(
            DEFAULT_ORG_ID, "mixpanel", "alice@co.com",
        ) is None


# ── get_connected_users: __admin__ exclusion ─────────────────────────
#
# ``get_connected_users`` feeds the admin Upstream Detail page's
# "Connected users" column. The ``__admin__`` sentinel lives in the
# same ``user:<upstream>:<user>`` keyspace as real users' tokens but
# must NOT surface in that UI — it's not a user.
#
# Both backends hide it with an explicit filter. Pinning the filter
# across both:
#
#   * keeps the two adapter implementations in sync (they drift easily
#     — file uses an iterator comprehension, mongo uses a list
#     comprehension over a regex-scan result),
#   * catches a refactor that stores admin tokens under a new key
#     prefix without also removing the now-dead filter (UI would
#     start listing ``__admin__`` as a connected user).


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_get_connected_users_excludes_admin_sentinel(
    backend: str, tmp_path: Path,
) -> None:
    async with _make_store(backend, tmp_path) as store:
        await store.put_user_token(
            DEFAULT_ORG_ID, "__admin__", "notion", _token("admin-at"),
        )
        await store.put_user_token(
            DEFAULT_ORG_ID, "alice@co.com", "notion", _token("alice-at"),
        )
        await store.put_user_token(
            DEFAULT_ORG_ID, "bob@co.com", "notion", _token("bob-at"),
        )

        connected = await store.get_connected_users(DEFAULT_ORG_ID, "notion")

        # Admin sentinel must never leak; real users listed alphabetically.
        assert "__admin__" not in connected
        assert sorted(connected) == ["alice@co.com", "bob@co.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_get_connected_users_returns_empty_when_only_admin(
    backend: str, tmp_path: Path,
) -> None:
    """An admin-only upstream surfaces zero "connected users" in the
    UI — the admin isn't a user. This is the common state after the
    admin has authenticated but no real user has yet."""
    async with _make_store(backend, tmp_path) as store:
        await store.put_user_token(
            DEFAULT_ORG_ID, "__admin__", "notion", _token("admin-at"),
        )
        connected = await store.get_connected_users(DEFAULT_ORG_ID, "notion")
        assert connected == []


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_connection_error_signature_roundtrip(
    backend: str, tmp_path: Path,
) -> None:
    """``set_connection_error`` accepts an optional signature dict that
    captures the upstream's refresh-failure response shape
    (``status_code``, ``body_excerpt``, ``error_code``, ``timestamp``).
    ``get_connection_error_signature`` reads it back — both adapters
    must agree so an operator reading ``db.connections.find`` in
    cloud mode sees the same shape a file-mode dev sees."""
    signature = {
        "status_code": 400,
        "body_excerpt": '{"error":"invalid_grant"}',
        "error_code": "invalid_grant",
        "timestamp": "2026-04-24T12:00:00+00:00",
    }
    async with _make_store(backend, tmp_path) as store:
        await store.set_connection_error(
            DEFAULT_ORG_ID, "notion", "token_refresh_failed",
            signature=signature,
        )
        assert await store.get_connection_error(
            DEFAULT_ORG_ID, "notion",
        ) == "token_refresh_failed"
        roundtripped = await store.get_connection_error_signature(
            DEFAULT_ORG_ID, "notion",
        )
        assert roundtripped == signature


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_connection_error_without_signature_reads_none(
    backend: str, tmp_path: Path,
) -> None:
    """Callers that don't capture a signature (e.g. ``connection_timeout``)
    must still land a plain error row. Reading the signature afterward
    returns ``None`` rather than an exception — the "no forensic
    signal" case is normal."""
    async with _make_store(backend, tmp_path) as store:
        await store.set_connection_error(
            DEFAULT_ORG_ID, "notion", "connection_timeout",
        )
        assert await store.get_connection_error(
            DEFAULT_ORG_ID, "notion",
        ) == "connection_timeout"
        assert await store.get_connection_error_signature(
            DEFAULT_ORG_ID, "notion",
        ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_refresh_failure_counter_increments(
    backend: str, tmp_path: Path,
) -> None:
    """``record_refresh_failure`` must increment monotonically and
    preserve the original ``first_failure_at`` across calls. That
    anchor is how §5.1's window check distinguishes "five failures
    in two minutes" (transient burst, keep) from "five failures
    over 30 min" (sustained, delete)."""
    async with _make_store(backend, tmp_path) as store:
        c1, f1 = await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert c1 == 1

        c2, f2 = await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert c2 == 2
        # Same anchor — a later call doesn't reset the window.
        assert f1 == f2

        read = await store.get_refresh_failures(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert read is not None
        assert read[0] == 2
        assert read[1] == f1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_refresh_failure_counter_isolated_per_user(
    backend: str, tmp_path: Path,
) -> None:
    """Alice's failing refresh must not poison Bob's counter — the
    key is ``(upstream, user)``, not ``upstream`` alone. Without
    this isolation, a single-user outage would drag every user past
    the deletion threshold together."""
    async with _make_store(backend, tmp_path) as store:
        await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert await store.get_refresh_failures(
            DEFAULT_ORG_ID, "notion", "bob@co.com",
        ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_refresh_failure_counter_reset(
    backend: str, tmp_path: Path,
) -> None:
    """A successful refresh calls ``reset_refresh_failures`` — the
    next burst must start from zero. Otherwise a recovery after
    a 4-failure burst would still count toward "5 strikes" on the
    next unrelated blip."""
    async with _make_store(backend, tmp_path) as store:
        await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        await store.reset_refresh_failures(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert await store.get_refresh_failures(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        ) is None

        # Next failure starts a new run with count=1.
        c, _ = await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert c == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_connection_error_clear_also_drops_signature(
    backend: str, tmp_path: Path,
) -> None:
    """After a successful reconnect we call ``clear_connection_error``.
    That must take the signature with it — leaving a stale signature
    pointing at a past failure would confuse forensics the next time
    refresh breaks."""
    async with _make_store(backend, tmp_path) as store:
        await store.set_connection_error(
            DEFAULT_ORG_ID, "notion", "token_refresh_failed",
            signature={"status_code": 400, "body_excerpt": "",
                       "error_code": "invalid_grant", "timestamp": ""},
        )
        await store.clear_connection_error(DEFAULT_ORG_ID, "notion")
        assert await store.get_connection_error(
            DEFAULT_ORG_ID, "notion",
        ) is None
        assert await store.get_connection_error_signature(
            DEFAULT_ORG_ID, "notion",
        ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_refresh_failure_signature_roundtrip(
    backend: str, tmp_path: Path,
) -> None:
    """§5.4's signature dict must survive a round-trip through the
    per-user failure row and be readable as
    ``get_refresh_failure_signature``. §5.2's email pipeline keys on
    this — without per-user storage, a per_user_oauth upstream where
    alice and bob both refresh concurrently would lose one user's
    signature to the other."""
    signature = {
        "status_code": 400,
        "body_excerpt": '{"error":"invalid_grant"}',
        "error_code": "invalid_grant",
        "timestamp": "2026-04-24T12:00:00+00:00",
    }
    async with _make_store(backend, tmp_path) as store:
        await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
            signature=signature,
        )
        read = await store.get_refresh_failure_signature(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert read == signature


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_refresh_failure_signature_preserved_across_increments(
    backend: str, tmp_path: Path,
) -> None:
    """A second ``record_refresh_failure`` call without a signature
    must NOT wipe the signature from the first call — otherwise
    §5.2's email policy loses the ``invalid_grant`` signal after one
    intervening transient retry."""
    signature = {
        "status_code": 400,
        "body_excerpt": '{"error":"invalid_grant"}',
        "error_code": "invalid_grant",
        "timestamp": "2026-04-24T12:00:00+00:00",
    }
    async with _make_store(backend, tmp_path) as store:
        await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
            signature=signature,
        )
        # Increment without a fresh signature.
        await store.record_refresh_failure(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        read = await store.get_refresh_failure_signature(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert read == signature


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_notified_flag_roundtrip(
    backend: str, tmp_path: Path,
) -> None:
    """The §5.2 notified marker must set, read, and clear reliably
    across both backends. Collapse of any of these to a no-op would
    silently break one of: the first notification (was_notified
    stuck True), the spam guard (stuck False), or the re-notification
    after recovery (clear no-op)."""
    async with _make_store(backend, tmp_path) as store:
        assert await store.was_notified(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        ) is False
        await store.mark_notified(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert await store.was_notified(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        ) is True
        await store.clear_notified(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        )
        assert await store.was_notified(
            DEFAULT_ORG_ID, "notion", "alice@co.com",
        ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_get_connected_users_scopes_by_upstream(
    backend: str, tmp_path: Path,
) -> None:
    """Regression guard: the filter must not accidentally collapse the
    upstream axis. alice@slack should not appear as connected to
    notion."""
    async with _make_store(backend, tmp_path) as store:
        await store.put_user_token(
            DEFAULT_ORG_ID, "alice@co.com", "slack", _token("alice-slack"),
        )
        await store.put_user_token(
            DEFAULT_ORG_ID, "bob@co.com", "notion", _token("bob-notion"),
        )

        assert await store.get_connected_users(DEFAULT_ORG_ID, "slack") == [
            "alice@co.com",
        ]
        assert await store.get_connected_users(DEFAULT_ORG_ID, "notion") == [
            "bob@co.com",
        ]


# ── comprehensive per-entity purge (delete_all_for_upstream / _user) ──
#
# An upstream remove + re-add on the same slug, or a user remove +
# re-invite with the same email, must NOT resurrect any stale OAuth
# state — in particular a dead DCR ``client_info`` row that would make
# the upstream's authorize endpoint reject our recycled ``client_id``
# with ``invalid_client``. The token-only ``delete_all_*_tokens`` helpers
# leave every sibling row behind; these purge the whole key family.


async def _seed_full_state(
    store: ConnectionStore, upstream: str, user: str,
) -> None:
    """Write one row of every key shape for (upstream, user) so a purge
    can be proven exhaustive. Covers both axes: user-scoped rows
    (``user``/``client_info``/``oauth_metadata``/``pending_code``/
    ``failures``/``notified``) and upstream-only rows
    (``admin``/``enabled``/``error``/``started_config_hash``)."""
    await store.put_user_token(DEFAULT_ORG_ID, user, upstream, _token())
    await store.put_admin_token(
        DEFAULT_ORG_ID, upstream, _token(), authorized_by="admin@co.com",
    )
    await store.put_client_info(
        DEFAULT_ORG_ID, upstream, user, {"client_id": f"cid-{upstream}-{user}"},
    )
    await store.put_oauth_metadata(
        DEFAULT_ORG_ID, upstream, user, {"issuer": f"iss-{upstream}"},
    )
    await store.put_pending_code(DEFAULT_ORG_ID, upstream, user, "code", "state")
    await store.record_refresh_failure(DEFAULT_ORG_ID, upstream, user)
    await store.mark_notified(DEFAULT_ORG_ID, upstream, user)
    await store.set_disabled(DEFAULT_ORG_ID, upstream)
    await store.set_connection_error(DEFAULT_ORG_ID, upstream, "boom")
    await store.set_started_config_hash(DEFAULT_ORG_ID, upstream, "hash-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_delete_all_for_upstream_purges_every_key_shape(
    backend: str, tmp_path: Path,
) -> None:
    """``delete_all_for_upstream`` must drop EVERY row keyed to the
    upstream — including the DCR ``client_info`` that the token-only
    cascade historically left behind (the ``invalid_client`` brick) —
    while leaving an unrelated upstream's rows untouched."""
    async with _make_store(backend, tmp_path) as store:
        await _seed_full_state(store, "github", "alice@co.com")
        await _seed_full_state(store, "github", "bob@co.com")
        await _seed_full_state(store, "slack", "alice@co.com")

        deleted = await store.delete_all_for_upstream(DEFAULT_ORG_ID, "github")
        assert deleted > 0

        # Every github row is gone, across both users and both axes.
        for user in ("alice@co.com", "bob@co.com"):
            assert await store.get_user_token(DEFAULT_ORG_ID, user, "github") is None
            assert await store.get_client_info(DEFAULT_ORG_ID, "github", user) is None
            assert await store.get_oauth_metadata(DEFAULT_ORG_ID, "github", user) is None
            assert await store.get_refresh_failures(DEFAULT_ORG_ID, "github", user) is None
            assert await store.was_notified(DEFAULT_ORG_ID, "github", user) is False
            assert await store.pop_pending_code(DEFAULT_ORG_ID, "github", user) is None
        assert await store.get_admin_token(DEFAULT_ORG_ID, "github") is None
        assert await store.is_enabled(DEFAULT_ORG_ID, "github") is True  # marker gone
        assert await store.get_connection_error(DEFAULT_ORG_ID, "github") is None
        assert await store.get_started_config_hash(DEFAULT_ORG_ID, "github") is None

        # The unrelated upstream is fully intact.
        assert await store.get_client_info(
            DEFAULT_ORG_ID, "slack", "alice@co.com",
        ) is not None
        assert await store.get_admin_token(DEFAULT_ORG_ID, "slack") is not None
        assert await store.is_enabled(DEFAULT_ORG_ID, "slack") is False

        # Idempotent: re-running finds nothing left.
        assert await store.delete_all_for_upstream(DEFAULT_ORG_ID, "github") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_delete_all_for_user_purges_user_axis_only(
    backend: str, tmp_path: Path,
) -> None:
    """``delete_all_for_user`` must drop every USER-scoped row for that
    email across all upstreams (so a re-invite re-registers cleanly),
    but must NOT touch upstream-only rows (admin token, disabled marker,
    connection error, started-config-hash) — those outlive any one user,
    and another user on the same upstream must be unaffected."""
    async with _make_store(backend, tmp_path) as store:
        await _seed_full_state(store, "github", "alice@co.com")
        await _seed_full_state(store, "slack", "alice@co.com")
        await _seed_full_state(store, "github", "bob@co.com")

        deleted = await store.delete_all_for_user(DEFAULT_ORG_ID, "alice@co.com")
        assert deleted > 0

        # All of alice's per-user rows are gone, on every upstream.
        for upstream in ("github", "slack"):
            assert await store.get_user_token(
                DEFAULT_ORG_ID, "alice@co.com", upstream,
            ) is None
            assert await store.get_client_info(
                DEFAULT_ORG_ID, upstream, "alice@co.com",
            ) is None
            assert await store.get_oauth_metadata(
                DEFAULT_ORG_ID, upstream, "alice@co.com",
            ) is None
            assert await store.get_refresh_failures(
                DEFAULT_ORG_ID, upstream, "alice@co.com",
            ) is None

        # Bob (same upstream) is untouched.
        assert await store.get_user_token(
            DEFAULT_ORG_ID, "bob@co.com", "github",
        ) is not None
        assert await store.get_client_info(
            DEFAULT_ORG_ID, "github", "bob@co.com",
        ) is not None

        # Upstream-only rows are NOT user-scoped — they survive.
        assert await store.get_admin_token(DEFAULT_ORG_ID, "github") is not None
        assert await store.is_enabled(DEFAULT_ORG_ID, "github") is False  # still disabled
        assert await store.get_connection_error(DEFAULT_ORG_ID, "github") is not None
        assert await store.get_started_config_hash(
            DEFAULT_ORG_ID, "github",
        ) == "hash-1"

        assert await store.delete_all_for_user(DEFAULT_ORG_ID, "alice@co.com") == 0


async def _seed_full_state_org(
    store: ConnectionStore, org_id: str, upstream: str, user: str,
) -> None:
    """Like ``_seed_full_state`` but for an explicit org — used to prove
    ``delete_all_for_org`` is org-scoped on the multi-tenant backend."""
    await store.put_user_token(org_id, user, upstream, _token())
    await store.put_admin_token(
        org_id, upstream, _token(), authorized_by="admin@co.com",
    )
    await store.put_client_info(
        org_id, upstream, user, {"client_id": f"cid-{upstream}-{user}"},
    )
    await store.put_oauth_metadata(
        org_id, upstream, user, {"issuer": f"iss-{upstream}"},
    )
    await store.put_pending_code(org_id, upstream, user, "code", "state")
    await store.record_refresh_failure(org_id, upstream, user)
    await store.mark_notified(org_id, upstream, user)
    await store.set_disabled(org_id, upstream)
    await store.set_connection_error(org_id, upstream, "boom")
    await store.set_started_config_hash(org_id, upstream, "hash-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_delete_all_for_org_purges_every_key_shape(
    backend: str, tmp_path: Path,
) -> None:
    """``delete_all_for_org`` must drop EVERY row for the org — across
    all upstreams and users, on both axes — leaving the store empty.
    This is the org-deletion cascade for the connection collection."""
    async with _make_store(backend, tmp_path) as store:
        await _seed_full_state(store, "github", "alice@co.com")
        await _seed_full_state(store, "slack", "bob@co.com")

        deleted = await store.delete_all_for_org(DEFAULT_ORG_ID)
        assert deleted > 0

        for upstream, user in (
            ("github", "alice@co.com"),
            ("slack", "bob@co.com"),
        ):
            assert await store.get_user_token(DEFAULT_ORG_ID, user, upstream) is None
            assert await store.get_admin_token(DEFAULT_ORG_ID, upstream) is None
            assert await store.get_client_info(
                DEFAULT_ORG_ID, upstream, user,
            ) is None
            assert await store.get_oauth_metadata(
                DEFAULT_ORG_ID, upstream, user,
            ) is None
            assert await store.get_refresh_failures(
                DEFAULT_ORG_ID, upstream, user,
            ) is None
            assert await store.was_notified(DEFAULT_ORG_ID, upstream, user) is False
            assert await store.pop_pending_code(
                DEFAULT_ORG_ID, upstream, user,
            ) is None
            assert await store.is_enabled(DEFAULT_ORG_ID, upstream) is True
            assert await store.get_connection_error(
                DEFAULT_ORG_ID, upstream,
            ) is None
            assert await store.get_started_config_hash(
                DEFAULT_ORG_ID, upstream,
            ) is None

        # Idempotent: nothing left to remove.
        assert await store.delete_all_for_org(DEFAULT_ORG_ID) == 0


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_delete_all_for_org_is_org_scoped() -> None:
    """On the multi-tenant Mongo backend, deleting one org leaves a
    second org's connection rows completely intact — even when both
    orgs share the same ``(upstream, user)`` synthetic keys."""
    async with temp_mongo_database() as db:
        encryptor = FieldEncryptor.from_master_secret("unit-test-key")
        coll = OrgScopedCollection(
            db[COLL_CONNECTIONS], COLL_CONNECTIONS, encryptor=encryptor,
        )
        store = MongoConnectionRepository(coll)
        await _seed_full_state_org(store, "org-doomed", "github", "alice@co.com")
        await _seed_full_state_org(store, "org-alive", "github", "alice@co.com")

        deleted = await store.delete_all_for_org("org-doomed")
        assert deleted > 0

        assert await store.get_admin_token("org-doomed", "github") is None
        assert await store.get_client_info(
            "org-doomed", "github", "alice@co.com",
        ) is None

        # org-alive is fully intact.
        assert await store.get_admin_token("org-alive", "github") is not None
        assert await store.get_client_info(
            "org-alive", "github", "alice@co.com",
        ) is not None
        assert await store.get_user_token(
            "org-alive", "alice@co.com", "github",
        ) is not None
