"""JSON-file backed ``OAuthStateRepository`` for standalone mode.

Single global ``oauth_state.json`` file — the gateway OAuth namespace
is not partitioned by org. Standalone mode never had a real
multi-tenant story for gateway tokens anyway.

A file has no size ceiling, so the state stays one file: each change
is applied to a copy kept in memory and the whole file is rewritten
atomically (temporary file, then rename). A failed write leaves the
previous file in place; the provider writes the same changes again
later, and that rewrite includes them.

Files written by older builds still load: tokens and approvals keep
their layout (``data_pruner`` edits the token sections too), and a
client stored as the bare registration reads as one that never
expires (``StoredClient.from_before_expiry``).
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog

from mcpolis.adapters.repositories.atomic_file import write_text_atomic
from mcpolis.adapters.repositories.oauth_state_codec import (
    ACCESS_TOKENS,
    CLIENT_APPROVALS,
    CLIENTS,
    REFRESH_TOKENS,
    ItemCodec,
    JsonObject,
)
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    OAuthStateRepository,
    OAuthStateSnapshot,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def _encode_kind[T](codec: ItemCodec[T], items: dict[str, T]) -> JsonObject:
    return {key: codec.encode(item) for key, item in items.items()}


class FileOAuthStateRepository(OAuthStateRepository):
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "oauth_state.json"
        self._state: OAuthStateSnapshot | None = None

    async def load(self) -> OAuthStateSnapshot:
        self._state = self._read()
        return self._state.copy()

    async def apply(self, changes: OAuthStateChanges) -> None:
        if self._state is None:
            self._state = self._read()
        self._state.apply(changes)
        self._write(self._state)

    def _read(self) -> OAuthStateSnapshot:
        if not self._path.exists():
            return OAuthStateSnapshot()
        try:
            data: object = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "oauth_state.read.failed",
                path=str(self._path),
            )
            return OAuthStateSnapshot()
        if not isinstance(data, dict):
            return OAuthStateSnapshot()
        raw: JsonObject = data  # pyright: ignore[reportUnknownVariableType]
        return OAuthStateSnapshot(
            clients=CLIENTS.decode_all(raw.get(CLIENTS.kind)),
            access_tokens=ACCESS_TOKENS.decode_all(raw.get(ACCESS_TOKENS.kind)),
            refresh_tokens=REFRESH_TOKENS.decode_all(raw.get(REFRESH_TOKENS.kind)),
            client_approvals=CLIENT_APPROVALS.decode_all(
                raw.get(CLIENT_APPROVALS.kind),
            ),
        )

    def _write(self, state: OAuthStateSnapshot) -> None:
        data: JsonObject = {
            CLIENTS.kind: _encode_kind(CLIENTS, state.clients),
            ACCESS_TOKENS.kind: _encode_kind(ACCESS_TOKENS, state.access_tokens),
            REFRESH_TOKENS.kind: _encode_kind(REFRESH_TOKENS, state.refresh_tokens),
            CLIENT_APPROVALS.kind: _encode_kind(
                CLIENT_APPROVALS, state.client_approvals,
            ),
        }
        write_text_atomic(self._path, json.dumps(data, indent=2))
