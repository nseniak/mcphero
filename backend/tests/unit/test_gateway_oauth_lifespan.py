"""The gateway sign-in store through the REAL cloud lifespan, on a
throwaway Mongo database (second review, finding 34: no test ran either
step through the app's own startup and shutdown):

1. at startup, the single document an older build wrote is converted
   (after the indexes that make the conversion safe to repeat), and kept
   for a rollback;
2. at shutdown, a revoke whose background deletion failed reaches
   storage before Mongo closes (the ``flush_gateway_sign_ins`` step).
"""
from __future__ import annotations

import asyncio
import secrets
import time

from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import McpGatewayOAuthProvider
from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.mongo_client import (
    COLL_GATEWAY_OAUTH,
    COLL_OAUTH_STATE,
    MotorDatabase,
)
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    OAuthStateRepository,
    OAuthStateSnapshot,
    StoredRefreshToken,
)
from mcpolis.entrypoints.app import create_app
from tests.unit.mongo_fixture import require_mongo, temp_mongo_database
from tests.unit.test_gateway_oauth_storage import old_document_json, write_old_document
from tests.unit.test_mcp_endpoints_start_at_boot import make_cloud_settings

MEMBER = "member@acme.test"


class FailsOnce(OAuthStateRepository):
    """The app's own store, whose next write fails once (a Mongo
    hiccup)."""

    def __init__(self, inner: OAuthStateRepository) -> None:
        self._inner = inner
        self.failures_left = 1

    async def load(self) -> OAuthStateSnapshot:
        return await self._inner.load()

    async def apply(self, changes: OAuthStateChanges) -> None:
        if self.failures_left:
            self.failures_left -= 1
            raise ConnectionError("mongo hiccup")
        await self._inner.apply(changes)


def make_claude_registration() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-claude",
        client_secret=secrets.token_hex(16),
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
    )


async def stored_kinds(db: MotorDatabase) -> list[str]:
    return sorted([doc["kind"] async for doc in db[COLL_GATEWAY_OAUTH].find({})])


async def test_the_real_lifespan_converts_at_startup_and_flushes_at_shutdown() -> None:
    mongo_uri = require_mongo()
    async with temp_mongo_database() as db:
        settings = make_cloud_settings(mongo_uri, db.name)
        encryptor = FieldEncryptor.from_master_secret(settings.encryption_key)
        claude = make_claude_registration()
        refresh = StoredRefreshToken(
            token=secrets.token_urlsafe(32), client_id="client-claude",
            user_email=MEMBER, scopes=[], created_at=time.time() - 3600,
        )
        await write_old_document(
            db, encryptor, old_document_json([claude], [], [refresh], []),
        )

        app = create_app(settings)
        provider: McpGatewayOAuthProvider = app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined]
        async with app.router.lifespan_context(app):
            # 1. Converted at startup, the old document kept.
            assert await db[COLL_OAUTH_STATE].count_documents({}) == 1
            assert await provider.load_refresh_token(claude, refresh.token) is not None
            await provider.mint_test_token(MEMBER)
            # 2. The revoke's background deletion fails once.
            store = FailsOnce(provider._state_repo)
            provider._state_repo = store
            assert provider.revoke_user_tokens(MEMBER) == 3
            for _ in range(20):
                await asyncio.sleep(0.01)
            assert store.failures_left == 0

        assert await stored_kinds(db) == ["clients", "clients", "conversion"], (
            "the revoke's deletion was not stored before Mongo closed"
        )
