from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from mcpolis.adapters.repositories.connection_store import ConnectionStore, OAuthToken
from mcpolis.domain.ports import ADMIN_USER_ID

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def _serialize_token(token: OAuthToken, authorized_by: str = "") -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "scopes": token.scopes,
        "refresh_token_created_at": (
            token.refresh_token_created_at.isoformat()
            if token.refresh_token_created_at
            else now.isoformat()
        ) if token.refresh_token else None,
        "authorized_by": authorized_by,
        "updated_at": now.isoformat(),
    }


def _deserialize_token(data: dict[str, Any]) -> OAuthToken:
    expires_at = None
    if data.get("expires_at"):
        expires_at = datetime.fromisoformat(data["expires_at"])
    refresh_token_created_at = None
    if data.get("refresh_token_created_at"):
        refresh_token_created_at = datetime.fromisoformat(
            data["refresh_token_created_at"]
        )
    updated_at = None
    if data.get("updated_at"):
        updated_at = datetime.fromisoformat(data["updated_at"])
    return OAuthToken(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=expires_at,
        scopes=data.get("scopes", []),
        refresh_token_created_at=refresh_token_created_at,
        updated_at=updated_at,
    )


def _key_upstream_id(key: str) -> str | None:
    """The upstream_id every connection-store key is built around: it is
    always the second colon-field of ``<prefix>:<upstream_id>[:<user_id>]``.
    ``None`` for a malformed single-field key."""
    parts = key.split(":")
    return parts[1] if len(parts) >= 2 else None


def _key_user_id(key: str) -> str | None:
    """The user_id of a user-scoped key, or ``None`` for an upstream-only
    key. User-scoped keys are exactly the three-field shape
    ``<prefix>:<upstream_id>:<user_id>``; emails and slugs never contain
    a colon, so a strict field count distinguishes the two axes."""
    parts = key.split(":")
    return parts[2] if len(parts) == 3 else None


class FileConnectionStore(ConnectionStore):
    """JSON file-based connection store with asyncio.Lock for concurrency."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "connections.json"
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "connection_store.read.failed",
                path=str(self._path),
            )
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))

    @staticmethod
    def _admin_key(upstream_id: str) -> str:
        return f"admin:{upstream_id}"

    @staticmethod
    def _user_key(user_id: str, upstream_id: str) -> str:
        return f"user:{upstream_id}:{user_id}"

    async def get_admin_token(self, org_id: str, upstream_id: str) -> OAuthToken | None:
        async with self._lock:
            data = self._read()
            entry = data.get(self._admin_key(upstream_id))
            if entry is None:
                return None
            return _deserialize_token(entry)

    async def put_admin_token(
        self, org_id: str, upstream_id: str, token: OAuthToken, authorized_by: str
    ) -> None:
        async with self._lock:
            data = self._read()
            data[self._admin_key(upstream_id)] = _serialize_token(token, authorized_by)
            self._write(data)

    async def get_user_token(self, org_id: str, user_id: str, upstream_id: str) -> OAuthToken | None:
        async with self._lock:
            data = self._read()
            entry = data.get(self._user_key(user_id, upstream_id))
            if entry is None:
                return None
            return _deserialize_token(entry)

    async def put_user_token(self, org_id: str, user_id: str, upstream_id: str, token: OAuthToken) -> None:
        async with self._lock:
            data = self._read()
            data[self._user_key(user_id, upstream_id)] = _serialize_token(token)
            self._write(data)

    async def delete_user_token(self, org_id: str, user_id: str, upstream_id: str) -> None:
        async with self._lock:
            data = self._read()
            data.pop(self._user_key(user_id, upstream_id), None)
            self._write(data)

    async def delete_all_user_tokens(self, org_id: str, user_id: str) -> int:
        async with self._lock:
            data = self._read()
            suffix = f":{user_id}"
            keys = [k for k in data if k.startswith("user:") and k.endswith(suffix)]
            for k in keys:
                del data[k]
            if keys:
                self._write(data)
            return len(keys)

    async def delete_all_upstream_tokens(self, org_id: str, upstream_id: str) -> int:
        async with self._lock:
            data = self._read()
            admin_key = self._admin_key(upstream_id)
            user_prefix = f"user:{upstream_id}:"
            keys = [
                k for k in data
                if k == admin_key or k.startswith(user_prefix)
            ]
            for k in keys:
                del data[k]
            if keys:
                self._write(data)
            return len(keys)

    async def delete_all_for_upstream(self, org_id: str, upstream_id: str) -> int:
        async with self._lock:
            data = self._read()
            keys = [k for k in data if _key_upstream_id(k) == upstream_id]
            for k in keys:
                del data[k]
            if keys:
                self._write(data)
            return len(keys)

    async def delete_all_for_user(self, org_id: str, user_id: str) -> int:
        async with self._lock:
            data = self._read()
            keys = [k for k in data if _key_user_id(k) == user_id]
            for k in keys:
                del data[k]
            if keys:
                self._write(data)
            return len(keys)

    async def delete_all_for_org(self, org_id: str) -> int:
        # The file store is single-org (standalone mode), so every key
        # belongs to this org: clearing the whole store IS the per-org
        # purge. No org prefix exists in the key space to filter on.
        async with self._lock:
            data = self._read()
            count = len(data)
            if count:
                self._write({})
            return count

    async def get_all_stored_tokens(self, org_id: str) -> list[tuple[str, str]]:
        async with self._lock:
            data = self._read()
            results: list[tuple[str, str]] = []
            for key in data:
                if not key.startswith("user:"):
                    continue
                # key format: "user:{upstream_id}:{user_id}"
                rest = key.removeprefix("user:")
                colon = rest.find(":")
                if colon < 0:
                    continue
                upstream_id = rest[:colon]
                user_id = rest[colon + 1:]
                results.append((upstream_id, user_id))
            return results

    async def get_connected_users(self, org_id: str, upstream_id: str) -> list[str]:
        prefix = f"user:{upstream_id}:"
        async with self._lock:
            data = self._read()
            return sorted(
                user_id
                for key in data
                if key.startswith(prefix)
                for user_id in [key[len(prefix):]]
                if user_id != ADMIN_USER_ID
            )

    @staticmethod
    def _client_info_key(upstream_id: str, user_id: str) -> str:
        return f"client_info:{upstream_id}:{user_id}"

    async def get_client_info(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            data = self._read()
            return data.get(self._client_info_key(upstream_id, user_id))

    async def put_client_info(
        self, org_id: str, upstream_id: str, user_id: str, client_info: dict[str, Any]
    ) -> None:
        async with self._lock:
            data = self._read()
            data[self._client_info_key(upstream_id, user_id)] = client_info
            self._write(data)

    async def delete_client_info(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> None:
        async with self._lock:
            data = self._read()
            if data.pop(self._client_info_key(upstream_id, user_id), None) is not None:
                self._write(data)

    @staticmethod
    def _oauth_metadata_key(upstream_id: str, user_id: str) -> str:
        return f"oauth_metadata:{upstream_id}:{user_id}"

    async def get_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            data = self._read()
            entry: Any = data.get(self._oauth_metadata_key(upstream_id, user_id))
            return dict(entry) if isinstance(entry, dict) else None  # pyright: ignore[reportUnknownArgumentType]

    async def put_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str,
        metadata: dict[str, Any],
    ) -> None:
        async with self._lock:
            data = self._read()
            data[self._oauth_metadata_key(upstream_id, user_id)] = metadata
            self._write(data)

    async def delete_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        async with self._lock:
            data = self._read()
            if data.pop(
                self._oauth_metadata_key(upstream_id, user_id), None,
            ) is not None:
                self._write(data)

    @staticmethod
    def _error_key(upstream_id: str) -> str:
        return f"error:{upstream_id}"

    async def get_connection_error(self, org_id: str, upstream_id: str) -> str | None:
        async with self._lock:
            data = self._read()
            entry: Any = data.get(self._error_key(upstream_id))
            if isinstance(entry, dict):
                msg: Any = entry.get("error")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                return msg if isinstance(msg, str) else None
            return entry if isinstance(entry, str) else None

    async def set_connection_error(
        self,
        org_id: str,
        upstream_id: str,
        error: str,
        signature: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            data = self._read()
            data[self._error_key(upstream_id)] = {
                "error": error,
                "signature": signature,
            }
            self._write(data)

    async def get_connection_error_signature(
        self, org_id: str, upstream_id: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            data = self._read()
            entry: Any = data.get(self._error_key(upstream_id))
            if isinstance(entry, dict):
                sig: Any = entry.get("signature")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(sig, dict):
                    return dict(sig)  # pyright: ignore[reportUnknownArgumentType]
            return None

    async def clear_connection_error(self, org_id: str, upstream_id: str) -> None:
        async with self._lock:
            data = self._read()
            data.pop(self._error_key(upstream_id), None)
            self._write(data)

    @staticmethod
    def _failures_key(upstream_id: str, user_id: str) -> str:
        return f"failures:{upstream_id}:{user_id}"

    @staticmethod
    def _notified_key(upstream_id: str, user_id: str) -> str:
        return f"notified:{upstream_id}:{user_id}"

    async def record_refresh_failure(
        self,
        org_id: str,
        upstream_id: str,
        user_id: str,
        *,
        signature: dict[str, Any] | None = None,
    ) -> tuple[int, datetime]:
        async with self._lock:
            data = self._read()
            key = self._failures_key(upstream_id, user_id)
            entry: Any = data.get(key)
            now = datetime.now(UTC)
            if isinstance(entry, dict):
                raw_count: Any = entry.get("count", 0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                count = int(raw_count) if isinstance(raw_count, int) else 0
                raw_first: Any = entry.get("first_failure_at")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(raw_first, str):
                    try:
                        first_at = datetime.fromisoformat(raw_first)
                    except ValueError:
                        first_at = now
                else:
                    first_at = now
            else:
                count = 0
                first_at = now
            count += 1
            record: dict[str, Any] = {
                "count": count,
                "first_failure_at": first_at.isoformat(),
            }
            if signature is not None:
                record["signature"] = signature
            elif isinstance(entry, dict):
                prior: Any = entry.get("signature")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(prior, dict):
                    record["signature"] = dict(prior)  # pyright: ignore[reportUnknownArgumentType]
            data[key] = record
            self._write(data)
            return count, first_at

    async def reset_refresh_failures(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        async with self._lock:
            data = self._read()
            if data.pop(self._failures_key(upstream_id, user_id), None) is not None:
                self._write(data)

    async def get_refresh_failures(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> tuple[int, datetime] | None:
        async with self._lock:
            data = self._read()
            entry: Any = data.get(self._failures_key(upstream_id, user_id))
            if not isinstance(entry, dict):
                return None
            raw_count: Any = entry.get("count")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            raw_first: Any = entry.get("first_failure_at")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if not isinstance(raw_count, int) or not isinstance(raw_first, str):
                return None
            try:
                first_at = datetime.fromisoformat(raw_first)
            except ValueError:
                return None
            return raw_count, first_at

    async def get_refresh_failure_signature(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            data = self._read()
            entry: Any = data.get(self._failures_key(upstream_id, user_id))
            if not isinstance(entry, dict):
                return None
            sig: Any = entry.get("signature")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(sig, dict):
                return dict(sig)  # pyright: ignore[reportUnknownArgumentType]
            return None

    async def mark_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        async with self._lock:
            data = self._read()
            data[self._notified_key(upstream_id, user_id)] = {
                "notified_at": datetime.now(UTC).isoformat(),
            }
            self._write(data)

    async def was_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> bool:
        async with self._lock:
            data = self._read()
            return self._notified_key(upstream_id, user_id) in data

    async def clear_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        async with self._lock:
            data = self._read()
            if data.pop(
                self._notified_key(upstream_id, user_id), None,
            ) is not None:
                self._write(data)

    @staticmethod
    def _enabled_key(upstream_id: str) -> str:
        return f"enabled:{upstream_id}"

    async def set_enabled(self, org_id: str, upstream_id: str) -> None:
        """Bistate semantic: remove any explicit-disabled marker so
        the upstream falls back to the default-enabled state.
        Idempotent. Phase E collapse — replaces the prior tristate
        where ``set_enabled`` wrote an explicit ``enabled: True`` and
        the now-deleted ``clear_enabled`` removed the marker entirely;
        the two collapse into one verb."""
        async with self._lock:
            data = self._read()
            data.pop(self._enabled_key(upstream_id), None)
            self._write(data)

    async def set_disabled(self, org_id: str, upstream_id: str) -> None:
        """Persist an explicit ``enabled: False`` so the boot
        reconciler skips this upstream across restarts. The only
        durable state stored — absence implies enabled."""
        async with self._lock:
            data = self._read()
            data[self._enabled_key(upstream_id)] = False
            self._write(data)

    async def is_enabled(self, org_id: str, upstream_id: str) -> bool:
        async with self._lock:
            data = self._read()
            return data.get(self._enabled_key(upstream_id)) is not False

    async def get_disabled_ids(self, org_id: str) -> set[str]:
        async with self._lock:
            data = self._read()
            return {
                key.removeprefix("enabled:")
                for key in data
                if key.startswith("enabled:") and data[key] is False
            }

    @staticmethod
    def _pending_code_key(upstream_id: str, user_id: str) -> str:
        return f"pending_code:{upstream_id}:{user_id}"

    async def put_pending_code(
        self, org_id: str, upstream_id: str, user_id: str, code: str, original_state: str
    ) -> None:
        async with self._lock:
            data = self._read()
            data[self._pending_code_key(upstream_id, user_id)] = {
                "code": code,
                "original_state": original_state,
            }
            self._write(data)

    async def pop_pending_code(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> tuple[str, str] | None:
        async with self._lock:
            data = self._read()
            key = self._pending_code_key(upstream_id, user_id)
            entry = data.pop(key, None)
            if entry is None:
                return None
            self._write(data)
            return (entry["code"], entry["original_state"])

    @staticmethod
    def _started_config_hash_key(upstream_id: str) -> str:
        return f"started_config_hash:{upstream_id}"

    async def get_started_config_hash(
        self, org_id: str, upstream_id: str,
    ) -> str | None:
        async with self._lock:
            data = self._read()
            value = data.get(self._started_config_hash_key(upstream_id))
            return value if isinstance(value, str) else None

    async def set_started_config_hash(
        self, org_id: str, upstream_id: str, config_hash: str,
    ) -> None:
        async with self._lock:
            data = self._read()
            data[self._started_config_hash_key(upstream_id)] = config_hash
            self._write(data)

    async def clear_started_config_hash(
        self, org_id: str, upstream_id: str,
    ) -> None:
        async with self._lock:
            data = self._read()
            data.pop(self._started_config_hash_key(upstream_id), None)
            self._write(data)
