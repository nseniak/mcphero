"""JSON-file backed ``OAuthStateRepository`` for standalone mode.

Single global ``oauth_state.json`` file — the gateway OAuth namespace
is no longer partitioned by org. Standalone mode never had a real
multi-tenant story for gateway tokens anyway.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog
from mcp.shared.auth import OAuthClientInformationFull

from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateRepository,
    OAuthStateSnapshot,
    StoredAccessToken,
    StoredRefreshToken,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Refresh tokens expire after this many seconds (also enforced at mint
# time in ``mcp_gateway_oauth_provider``). Loaded tokens beyond the TTL are
# dropped on load so a restart cleans up expired state.
REFRESH_TOKEN_TTL = 30 * 86400


class FileOAuthStateRepository(OAuthStateRepository):
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "oauth_state.json"

    async def load(self) -> OAuthStateSnapshot:
        if not self._path.exists():
            return OAuthStateSnapshot()
        try:
            data: dict[str, Any] = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "oauth_state.read.failed",
                path=str(self._path),
            )
            return OAuthStateSnapshot()

        snapshot = OAuthStateSnapshot()
        now = int(time.time())

        for client_id, client_data in data.get("clients", {}).items():
            try:
                snapshot.clients[client_id] = (
                    OAuthClientInformationFull.model_validate(client_data)
                )
            except Exception:
                logger.warning(
                    "oauth_state.client_registration.invalid",
                    client_id=client_id,
                )

        for token_str, token_data in data.get("access_tokens", {}).items():
            stored = StoredAccessToken(
                token=token_data["token"],
                client_id=token_data["client_id"],
                user_email=token_data["user_email"],
                scopes=token_data.get("scopes", []),
                expires_at=token_data["expires_at"],
            )
            if stored.expires_at > now:
                snapshot.access_tokens[token_str] = stored

        for token_str, token_data in data.get("refresh_tokens", {}).items():
            stored_r = StoredRefreshToken(
                token=token_data["token"],
                client_id=token_data["client_id"],
                user_email=token_data["user_email"],
                scopes=token_data.get("scopes", []),
                created_at=token_data["created_at"],
            )
            if time.time() - stored_r.created_at <= REFRESH_TOKEN_TTL:
                snapshot.refresh_tokens[token_str] = stored_r

        logger.info(
            "oauth_state.loaded",
            clients_count=len(snapshot.clients),
            access_tokens_count=len(snapshot.access_tokens),
            refresh_tokens_count=len(snapshot.refresh_tokens),
        )
        return snapshot

    async def save(self, snapshot: OAuthStateSnapshot) -> None:
        data: dict[str, Any] = {
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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)
