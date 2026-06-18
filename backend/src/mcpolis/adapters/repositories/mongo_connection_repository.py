"""Mongo-backed ``ConnectionRepository``.

Storage model: one document per logical key in the ``connections``
collection. Each doc has ``org_id`` (enforced by ``OrgScopedCollection``)
plus a synthetic ``key`` string that mirrors the file-store key space
(``admin:<upstream_id>``, ``user:<upstream_id>:<user_id>``,
``client_info:<upstream_id>:<user_id>``, etc.).

Encryption: token blobs are stored under a ``token`` sub-document and
the sensitive fields (access_token, refresh_token) are listed in
``ENCRYPTED_FIELDS[COLL_CONNECTIONS]`` in ``mongo_client.py``. The
``OrgScopedCollection`` wrapper transparently encrypts on write and
decrypts on read.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
    OAuthToken,
)
from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.domain.ports import ADMIN_USER_ID
from mcpolis.domain.ports.connection_repository import ConnectionRepository


def _serialize_token(token: OAuthToken) -> dict[str, Any]:
    # Always stamp ``updated_at`` with the current write time so the
    # max-age ceiling (TOKEN_MAX_AGE_SECONDS in oauth_refresh) can
    # see freshness independent of the upstream-declared expires_at.
    return {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "scopes": list(token.scopes),
        "refresh_token_created_at": (
            token.refresh_token_created_at.isoformat()
            if token.refresh_token_created_at
            else None
        ),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _deserialize_token(data: dict[str, Any]) -> OAuthToken:
    expires_at_raw = data.get("expires_at")
    expires_at = (
        datetime.fromisoformat(expires_at_raw) if expires_at_raw else None
    )
    refresh_created_raw = data.get("refresh_token_created_at")
    refresh_created = (
        datetime.fromisoformat(refresh_created_raw)
        if refresh_created_raw
        else None
    )
    updated_raw = data.get("updated_at")
    updated_at = (
        datetime.fromisoformat(updated_raw) if updated_raw else None
    )
    return OAuthToken(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=expires_at,
        scopes=list(data.get("scopes") or []),
        refresh_token_created_at=refresh_created,
        updated_at=updated_at,
    )


def _admin_key(upstream_id: str) -> str:
    return f"admin:{upstream_id}"


def _user_key(user_id: str, upstream_id: str) -> str:
    return f"user:{upstream_id}:{user_id}"


def _client_info_key(upstream_id: str, user_id: str) -> str:
    return f"client_info:{upstream_id}:{user_id}"


def _oauth_metadata_key(upstream_id: str, user_id: str) -> str:
    return f"oauth_metadata:{upstream_id}:{user_id}"


def _error_key(upstream_id: str) -> str:
    return f"error:{upstream_id}"


def _enabled_key(upstream_id: str) -> str:
    return f"enabled:{upstream_id}"


def _pending_code_key(upstream_id: str, user_id: str) -> str:
    return f"pending_code:{upstream_id}:{user_id}"


def _failures_key(upstream_id: str, user_id: str) -> str:
    return f"failures:{upstream_id}:{user_id}"


def _notified_key(upstream_id: str, user_id: str) -> str:
    return f"notified:{upstream_id}:{user_id}"


def _started_config_hash_key(upstream_id: str) -> str:
    return f"started_config_hash:{upstream_id}"


class MongoConnectionRepository(ConnectionStore, ConnectionRepository):
    """Implements both the new ``ConnectionRepository`` protocol and the
    legacy ``ConnectionStore`` abstract class — the latter is still
    referenced by a handful of call sites that haven't migrated to the
    protocol yet. Behavior is identical; only the imports differ."""

    def __init__(self, collection: OrgScopedCollection) -> None:
        self._coll = collection

    # --- Admin tokens ---

    async def get_admin_token(
        self, org_id: str, upstream_id: str
    ) -> OAuthToken | None:
        doc = await self._coll.find_one(org_id, {"key": _admin_key(upstream_id)})
        if doc is None or "token" not in doc:
            return None
        return _deserialize_token(doc["token"])

    async def put_admin_token(
        self, org_id: str, upstream_id: str, token: OAuthToken, authorized_by: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        await self._coll.replace_one(
            org_id,
            {"key": _admin_key(upstream_id)},
            {
                "key": _admin_key(upstream_id),
                "token": _serialize_token(token),
                "authorized_by": authorized_by,
                "updated_at": now,
            },
            upsert=True,
        )

    # --- User tokens ---

    async def get_user_token(
        self, org_id: str, user_id: str, upstream_id: str
    ) -> OAuthToken | None:
        doc = await self._coll.find_one(
            org_id, {"key": _user_key(user_id, upstream_id)}
        )
        if doc is None or "token" not in doc:
            return None
        return _deserialize_token(doc["token"])

    async def put_user_token(
        self, org_id: str, user_id: str, upstream_id: str, token: OAuthToken
    ) -> None:
        now = datetime.now(UTC).isoformat()
        await self._coll.replace_one(
            org_id,
            {"key": _user_key(user_id, upstream_id)},
            {
                "key": _user_key(user_id, upstream_id),
                "token": _serialize_token(token),
                "updated_at": now,
            },
            upsert=True,
        )

    async def delete_user_token(
        self, org_id: str, user_id: str, upstream_id: str
    ) -> None:
        await self._coll.delete_one(
            org_id, {"key": _user_key(user_id, upstream_id)}
        )

    async def delete_all_user_tokens(self, org_id: str, user_id: str) -> int:
        # Find every user-token doc for this user and delete individually
        # so we can return an accurate count. The `key` field is indexed,
        # so the regex scan is bounded.
        import re
        pattern = re.compile(rf"^user:[^:]+:{re.escape(user_id)}$")
        docs = await self._coll.find_many(org_id, {"key": {"$regex": pattern.pattern}})
        count = 0
        for doc in docs:
            await self._coll.delete_one(org_id, {"key": doc["key"]})
            count += 1
        return count

    async def delete_all_upstream_tokens(
        self, org_id: str, upstream_id: str
    ) -> int:
        import re
        admin_key = _admin_key(upstream_id)
        user_pattern = re.compile(rf"^user:{re.escape(upstream_id)}:")
        admin_doc = await self._coll.find_one(org_id, {"key": admin_key})
        user_docs = await self._coll.find_many(
            org_id, {"key": {"$regex": user_pattern.pattern}}
        )
        count = 0
        if admin_doc is not None:
            await self._coll.delete_one(org_id, {"key": admin_key})
            count += 1
        for doc in user_docs:
            await self._coll.delete_one(org_id, {"key": doc["key"]})
            count += 1
        return count

    async def delete_all_for_upstream(
        self, org_id: str, upstream_id: str
    ) -> int:
        # Every key in the connection store is shaped
        # ``<prefix>:<upstream_id>[:<user_id>]`` — the upstream_id is
        # always the second colon-field. Match that field exactly so a
        # purge of ``github`` can never catch ``user:gitlab:github@x``.
        import re
        pattern = rf"^[^:]+:{re.escape(upstream_id)}(?::|$)"
        docs = await self._coll.find_many(org_id, {"key": {"$regex": pattern}})
        count = 0
        for doc in docs:
            await self._coll.delete_one(org_id, {"key": doc["key"]})
            count += 1
        return count

    async def delete_all_for_user(
        self, org_id: str, user_id: str
    ) -> int:
        # User-scoped keys are exactly the three-field shape
        # ``<prefix>:<upstream_id>:<user_id>``; the user_id is the whole
        # final field. Two-field upstream-only keys (admin/error/
        # enabled/started_config_hash) don't match, so they survive.
        import re
        pattern = rf"^[^:]+:[^:]+:{re.escape(user_id)}$"
        docs = await self._coll.find_many(org_id, {"key": {"$regex": pattern}})
        count = 0
        for doc in docs:
            await self._coll.delete_one(org_id, {"key": doc["key"]})
            count += 1
        return count

    async def get_all_stored_tokens(
        self, org_id: str
    ) -> list[tuple[str, str]]:
        docs = await self._coll.find_many(
            org_id, {"key": {"$regex": r"^user:"}}
        )
        results: list[tuple[str, str]] = []
        for doc in docs:
            key = doc.get("key")
            if not isinstance(key, str):
                continue
            rest = key.removeprefix("user:")
            colon = rest.find(":")
            if colon < 0:
                continue
            upstream_id = rest[:colon]
            user_id = rest[colon + 1:]
            results.append((upstream_id, user_id))
        return results

    async def get_connected_users(
        self, org_id: str, upstream_id: str
    ) -> list[str]:
        prefix = f"user:{upstream_id}:"
        docs = await self._coll.find_many(
            org_id, {"key": {"$regex": f"^{prefix}"}}
        )
        users: set[str] = set()
        for doc in docs:
            key = doc.get("key")
            if not isinstance(key, str):
                continue
            user_id = key[len(prefix):]
            if user_id and user_id != ADMIN_USER_ID:
                users.add(user_id)
        return sorted(users)

    # --- Client info ---

    async def get_client_info(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> dict[str, Any] | None:
        doc = await self._coll.find_one(
            org_id, {"key": _client_info_key(upstream_id, user_id)}
        )
        if doc is None:
            return None
        info = doc.get("client_info")
        return dict(info) if isinstance(info, dict) else None  # pyright: ignore[reportUnknownArgumentType]

    async def put_client_info(
        self, org_id: str, upstream_id: str, user_id: str, client_info: dict[str, Any]
    ) -> None:
        await self._coll.replace_one(
            org_id,
            {"key": _client_info_key(upstream_id, user_id)},
            {
                "key": _client_info_key(upstream_id, user_id),
                "client_info": client_info,
            },
            upsert=True,
        )

    async def delete_client_info(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> None:
        await self._coll.delete_one(
            org_id, {"key": _client_info_key(upstream_id, user_id)}
        )

    # --- OAuth authorization-server metadata (RFC 8414) ---

    async def get_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> dict[str, Any] | None:
        doc = await self._coll.find_one(
            org_id, {"key": _oauth_metadata_key(upstream_id, user_id)}
        )
        if doc is None:
            return None
        meta = doc.get("oauth_metadata")
        return dict(meta) if isinstance(meta, dict) else None  # pyright: ignore[reportUnknownArgumentType]

    async def put_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str,
        metadata: dict[str, Any],
    ) -> None:
        await self._coll.replace_one(
            org_id,
            {"key": _oauth_metadata_key(upstream_id, user_id)},
            {
                "key": _oauth_metadata_key(upstream_id, user_id),
                "oauth_metadata": metadata,
            },
            upsert=True,
        )

    async def delete_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> None:
        await self._coll.delete_one(
            org_id, {"key": _oauth_metadata_key(upstream_id, user_id)}
        )

    # --- Connection errors ---

    async def get_connection_error(
        self, org_id: str, upstream_id: str
    ) -> str | None:
        doc = await self._coll.find_one(
            org_id, {"key": _error_key(upstream_id)}
        )
        if doc is None:
            return None
        msg = doc.get("error")
        return msg if isinstance(msg, str) else None

    async def set_connection_error(
        self,
        org_id: str,
        upstream_id: str,
        error: str,
        signature: dict[str, Any] | None = None,
    ) -> None:
        await self._coll.replace_one(
            org_id,
            {"key": _error_key(upstream_id)},
            {
                "key": _error_key(upstream_id),
                "error": error,
                "signature": signature,
            },
            upsert=True,
        )

    async def get_connection_error_signature(
        self, org_id: str, upstream_id: str,
    ) -> dict[str, Any] | None:
        doc = await self._coll.find_one(
            org_id, {"key": _error_key(upstream_id)}
        )
        if doc is None:
            return None
        sig: Any = doc.get("signature")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(sig, dict):
            return dict(sig)  # pyright: ignore[reportUnknownArgumentType]
        return None

    async def clear_connection_error(
        self, org_id: str, upstream_id: str
    ) -> None:
        await self._coll.delete_one(
            org_id, {"key": _error_key(upstream_id)}
        )

    # --- Refresh-failure counters (§5.1) ---

    async def record_refresh_failure(
        self,
        org_id: str,
        upstream_id: str,
        user_id: str,
        *,
        signature: dict[str, Any] | None = None,
    ) -> tuple[int, datetime]:
        doc = await self._coll.find_one(
            org_id, {"key": _failures_key(upstream_id, user_id)}
        )
        now = datetime.now(UTC)
        prior_signature: dict[str, Any] | None = None
        if doc is None:
            count = 1
            first_at = now
        else:
            raw_count: Any = doc.get("count", 0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            count = int(raw_count) if isinstance(raw_count, int) else 0
            count += 1
            raw_first: Any = doc.get("first_failure_at")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(raw_first, str):
                try:
                    first_at = datetime.fromisoformat(raw_first)
                except ValueError:
                    first_at = now
            else:
                first_at = now
            prior_any: Any = doc.get("signature")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(prior_any, dict):
                prior_signature = dict(prior_any)  # pyright: ignore[reportUnknownArgumentType]
        record: dict[str, Any] = {
            "key": _failures_key(upstream_id, user_id),
            "count": count,
            "first_failure_at": first_at.isoformat(),
        }
        effective_signature = (
            signature if signature is not None else prior_signature
        )
        if effective_signature is not None:
            record["signature"] = effective_signature
        await self._coll.replace_one(
            org_id,
            {"key": _failures_key(upstream_id, user_id)},
            record,
            upsert=True,
        )
        return count, first_at

    async def reset_refresh_failures(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        await self._coll.delete_one(
            org_id, {"key": _failures_key(upstream_id, user_id)}
        )

    async def get_refresh_failures(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> tuple[int, datetime] | None:
        doc = await self._coll.find_one(
            org_id, {"key": _failures_key(upstream_id, user_id)}
        )
        if doc is None:
            return None
        raw_count: Any = doc.get("count")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        raw_first: Any = doc.get("first_failure_at")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
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
        doc = await self._coll.find_one(
            org_id, {"key": _failures_key(upstream_id, user_id)}
        )
        if doc is None:
            return None
        sig: Any = doc.get("signature")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(sig, dict):
            return dict(sig)  # pyright: ignore[reportUnknownArgumentType]
        return None

    # --- §5.2 notification tracker ---

    async def mark_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        await self._coll.replace_one(
            org_id,
            {"key": _notified_key(upstream_id, user_id)},
            {
                "key": _notified_key(upstream_id, user_id),
                "notified_at": datetime.now(UTC).isoformat(),
            },
            upsert=True,
        )

    async def was_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> bool:
        doc = await self._coll.find_one(
            org_id, {"key": _notified_key(upstream_id, user_id)}
        )
        return doc is not None

    async def clear_notified(
        self, org_id: str, upstream_id: str, user_id: str,
    ) -> None:
        await self._coll.delete_one(
            org_id, {"key": _notified_key(upstream_id, user_id)}
        )

    # --- Enabled flags (Phase E bistate) ---

    async def set_enabled(self, org_id: str, upstream_id: str) -> None:
        """Bistate semantic: remove any explicit-disabled marker so
        the upstream falls back to the default-enabled state.
        Idempotent. Phase E collapse — replaces the prior tristate
        where ``set_enabled`` wrote an explicit ``enabled: True`` and
        the now-deleted ``clear_enabled`` removed the marker entirely;
        the two collapse into one verb. Legacy ``enabled: True``
        rows in prod from the prior tristate are inert under this
        semantic."""
        await self._coll.delete_one(
            org_id, {"key": _enabled_key(upstream_id)}
        )

    async def set_disabled(self, org_id: str, upstream_id: str) -> None:
        """Persist an explicit ``enabled: False`` so the boot
        reconciler skips this upstream across restarts. The only
        durable state stored — absence implies enabled."""
        await self._coll.replace_one(
            org_id,
            {"key": _enabled_key(upstream_id)},
            {"key": _enabled_key(upstream_id), "enabled": False},
            upsert=True,
        )

    async def is_enabled(self, org_id: str, upstream_id: str) -> bool:
        doc = await self._coll.find_one(
            org_id, {"key": _enabled_key(upstream_id)}
        )
        if doc is None:
            return True
        return doc.get("enabled") is not False

    async def get_disabled_ids(self, org_id: str) -> set[str]:
        docs = await self._coll.find_many(
            org_id, {"key": {"$regex": r"^enabled:"}}
        )
        ids: set[str] = set()
        for doc in docs:
            key = doc.get("key")
            if not isinstance(key, str):
                continue
            if doc.get("enabled") is False:
                ids.add(key.removeprefix("enabled:"))
        return ids

    # --- Pending codes ---

    async def put_pending_code(
        self, org_id: str, upstream_id: str, user_id: str, code: str, original_state: str
    ) -> None:
        await self._coll.replace_one(
            org_id,
            {"key": _pending_code_key(upstream_id, user_id)},
            {
                "key": _pending_code_key(upstream_id, user_id),
                "code": code,
                "original_state": original_state,
            },
            upsert=True,
        )

    async def pop_pending_code(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> tuple[str, str] | None:
        doc = await self._coll.find_one(
            org_id, {"key": _pending_code_key(upstream_id, user_id)}
        )
        if doc is None:
            return None
        await self._coll.delete_one(
            org_id, {"key": _pending_code_key(upstream_id, user_id)}
        )
        code = doc.get("code")
        state = doc.get("original_state")
        if not isinstance(code, str) or not isinstance(state, str):
            return None
        return (code, state)

    # --- started_config_hash ---

    async def get_started_config_hash(
        self, org_id: str, upstream_id: str,
    ) -> str | None:
        doc = await self._coll.find_one(
            org_id, {"key": _started_config_hash_key(upstream_id)},
        )
        if doc is None:
            return None
        value = doc.get("hash")
        return value if isinstance(value, str) else None

    async def set_started_config_hash(
        self, org_id: str, upstream_id: str, config_hash: str,
    ) -> None:
        await self._coll.replace_one(
            org_id,
            {"key": _started_config_hash_key(upstream_id)},
            {
                "key": _started_config_hash_key(upstream_id),
                "hash": config_hash,
            },
            upsert=True,
        )

    async def clear_started_config_hash(
        self, org_id: str, upstream_id: str,
    ) -> None:
        await self._coll.delete_one(
            org_id, {"key": _started_config_hash_key(upstream_id)},
        )
