"""Mongo-backed ``OAuthStateRepository``.

The gateway OAuth state is small (kilobytes at most) and rarely
accessed individually — ``McpGatewayOAuthProvider`` loads the full snapshot
into memory at startup and saves after every mutation. So the Mongo
layout is a single document in the ``oauth_state`` collection, with
the entire snapshot serialized to JSON and AES-GCM-encrypted before
hitting the disk. That way *every* sensitive field (clients secrets,
access tokens, refresh tokens, user emails) is protected at rest
without having to enumerate them one by one.

The state is global, not partitioned by org: gateway tokens identify a
user, and the org dimension is resolved per request from the URL or
from the user's memberships.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog
from mcp.shared.auth import OAuthClientInformationFull

from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateRepository,
    OAuthStateSnapshot,
    StoredAccessToken,
    StoredRefreshToken,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

REFRESH_TOKEN_TTL = 30 * 86400

# Single fixed key for the global gateway OAuth snapshot. The
# underlying ``OrgScopedCollection`` is a thin wrapper around a Mongo
# collection that stores one document per "org_id" — we reuse it with
# a constant key rather than introduce a separate non-org-scoped
# collection abstraction just for this one document.
_GLOBAL_KEY = DEFAULT_ORG_ID


def _as_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(item, str):
            out.append(item)
    return out


class MongoOAuthStateRepository(OAuthStateRepository):
    def __init__(
        self, collection: OrgScopedCollection, encryptor: FieldEncryptor
    ) -> None:
        self._coll = collection
        # The OrgScopedCollection's built-in field encryption only
        # covers the ``encrypted_payload`` field, and that's where we
        # write the entire serialized snapshot. The encryptor here is
        # used directly so we can round-trip the payload ourselves.
        self._encryptor = encryptor

    async def load(self) -> OAuthStateSnapshot:
        doc = await self._coll.find_one(_GLOBAL_KEY)
        if doc is None:
            return OAuthStateSnapshot()
        payload = doc.get("encrypted_payload")
        if not isinstance(payload, str):
            return OAuthStateSnapshot()
        parsed: Any = json.loads(self._encryptor.decrypt_string(payload))
        if not isinstance(parsed, dict):
            return OAuthStateSnapshot()
        raw: dict[str, Any] = dict(parsed)  # pyright: ignore[reportUnknownArgumentType]
        return _deserialize(raw)

    async def save(self, snapshot: OAuthStateSnapshot) -> None:
        raw = _serialize(snapshot)
        blob = self._encryptor.encrypt_string(json.dumps(raw))
        await self._coll.replace_one(
            _GLOBAL_KEY,
            {},
            {"encrypted_payload": blob},
            upsert=True,
        )


def _serialize(snapshot: OAuthStateSnapshot) -> dict[str, Any]:
    return {
        "clients": {
            cid: client.model_dump(mode="json")
            for cid, client in snapshot.clients.items()
        },
        "access_tokens": {
            t: {
                "token": s.token,
                "client_id": s.client_id,
                "user_email": s.user_email,
                "scopes": s.scopes,
                "expires_at": s.expires_at,
            }
            for t, s in snapshot.access_tokens.items()
        },
        "refresh_tokens": {
            t: {
                "token": s.token,
                "client_id": s.client_id,
                "user_email": s.user_email,
                "scopes": s.scopes,
                "created_at": s.created_at,
            }
            for t, s in snapshot.refresh_tokens.items()
        },
    }


def _deserialize(raw: dict[str, Any]) -> OAuthStateSnapshot:
    snapshot = OAuthStateSnapshot()
    now = int(time.time())

    clients_raw: Any = raw.get("clients") or {}
    if isinstance(clients_raw, dict):
        for client_id_raw, client_data in clients_raw.items():  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(client_id_raw, str):
                continue
            try:
                snapshot.clients[client_id_raw] = (
                    OAuthClientInformationFull.model_validate(client_data)
                )
            except Exception:
                logger.warning(
                    "oauth_state.client_registration.invalid",
                    client_id=client_id_raw,
                )

    access_raw: Any = raw.get("access_tokens") or {}
    if isinstance(access_raw, dict):
        for token_str_raw, token_data_raw in access_raw.items():  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(token_str_raw, str) or not isinstance(token_data_raw, dict):
                continue
            td: dict[str, Any] = dict(token_data_raw)  # pyright: ignore[reportUnknownArgumentType]
            scopes: list[str] = _as_str_list(td.get("scopes"))
            stored = StoredAccessToken(
                token=str(td["token"]),
                client_id=str(td["client_id"]),
                user_email=str(td["user_email"]),
                scopes=scopes,
                expires_at=int(td["expires_at"]),
            )
            if stored.expires_at > now:
                snapshot.access_tokens[token_str_raw] = stored

    refresh_raw: Any = raw.get("refresh_tokens") or {}
    if isinstance(refresh_raw, dict):
        for token_str_raw, token_data_raw in refresh_raw.items():  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(token_str_raw, str) or not isinstance(token_data_raw, dict):
                continue
            td2: dict[str, Any] = dict(token_data_raw)  # pyright: ignore[reportUnknownArgumentType]
            scopes2: list[str] = _as_str_list(td2.get("scopes"))
            stored_r = StoredRefreshToken(
                token=str(td2["token"]),
                client_id=str(td2["client_id"]),
                user_email=str(td2["user_email"]),
                scopes=scopes2,
                created_at=float(td2["created_at"]),
            )
            if time.time() - stored_r.created_at <= REFRESH_TOKEN_TTL:
                snapshot.refresh_tokens[token_str_raw] = stored_r

    return snapshot
