"""Stand-ins for the gateway sign-in store (``OAuthStateRepository``),
and the provider built on one.

``InMemoryOAuthStateRepository.stored`` is what a restart would load.
``ScriptedOAuthStateRepository`` adds what a real database does now and
then: a write that hangs (held at a ``Gate`` until the test opens it)
and a write that fails.
"""
from __future__ import annotations

from collections.abc import Callable

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    WRITE_RETRY_FIRST_DELAY,
    McpGatewayOAuthProvider,
    SignInLimits,
)
from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.mongo_client import (
    COLL_GATEWAY_OAUTH,
    COLL_OAUTH_STATE,
    MotorDatabase,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_oauth_state_repository import (
    MongoOAuthStateRepository,
)
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    OAuthStateRepository,
    OAuthStateSnapshot,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from tests.unit.factories import Gate, make_runtime_manager


class InMemoryOAuthStateRepository(OAuthStateRepository):
    """Minimal in-memory stand-in: ``stored`` is what a restart loads."""

    def __init__(self) -> None:
        self.stored = OAuthStateSnapshot()

    async def load(self) -> OAuthStateSnapshot:
        return self.stored.copy()

    async def apply(self, changes: OAuthStateChanges) -> None:
        self.stored.apply(changes)


class StoreUnavailable(Exception):
    """A write the scripted store refused."""


class ScriptedOAuthStateRepository(InMemoryOAuthStateRepository):
    """In-memory store whose next write can hang or fail.

    * ``hold_next_write()`` — the next write waits at the returned gate
      (``gate.reached`` once it is there) and lands when the test sets
      ``gate.release``, like a write stuck on a slow connection.
    * ``fail_writes`` — that many next writes fail with
      ``StoreUnavailable``, storing nothing.
    * ``lose_answers`` — that many next writes are stored, then fail
      anyway (the database applied it, the answer never came back).
    * ``refuse`` — writes it returns True for fail, every time.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_writes = 0
        self.lose_answers = 0
        self.refuse: Callable[[OAuthStateChanges], bool] = lambda _changes: False
        self.writes: list[OAuthStateChanges] = []
        self._gate: Gate | None = None

    def hold_next_write(self) -> Gate:
        self._gate = Gate()
        return self._gate

    async def apply(self, changes: OAuthStateChanges) -> None:
        gate, self._gate = self._gate, None
        if gate is not None:
            await gate.hold()
        if self.fail_writes:
            self.fail_writes -= 1
            raise StoreUnavailable("the store is unavailable")
        if self.refuse(changes):
            raise StoreUnavailable("the store refused this write")
        self.writes.append(changes)
        await super().apply(changes)
        if self.lose_answers:
            self.lose_answers -= 1
            raise StoreUnavailable("the answer from the store was lost")


def make_gateway_provider(
    repo: OAuthStateRepository,
    config: SettingsConfig | None = None,
    limits: SignInLimits | None = None,
    write_retry_delay: float = WRITE_RETRY_FIRST_DELAY,
) -> McpGatewayOAuthProvider:
    """The gateway sign-in provider on ``repo``; a second one on the
    same store is the backend after a restart."""
    return McpGatewayOAuthProvider(
        google_client_id="test-google-client-id",
        google_client_secret="test-google-secret",
        server_url="http://127.0.0.1:8000",
        runtime_manager=make_runtime_manager(PolicyEngine(config or SettingsConfig())),
        state_repository=repo,
        limits=limits,
        write_retry_delay=write_retry_delay,
    )


def make_encryptor() -> FieldEncryptor:
    return FieldEncryptor.from_master_secret("cloud-master")


def make_mongo_oauth_state_repository(
    db: MotorDatabase, encryptor: FieldEncryptor,
) -> MongoOAuthStateRepository:
    """Wired as ``storage_factory.build_cloud_storage`` wires it."""
    return MongoOAuthStateRepository(
        OrgScopedCollection(
            db[COLL_GATEWAY_OAUTH], COLL_GATEWAY_OAUTH, encryptor=encryptor,
        ),
        OrgScopedCollection(
            db[COLL_OAUTH_STATE], COLL_OAUTH_STATE, encryptor=encryptor,
        ),
        encryptor,
    )
