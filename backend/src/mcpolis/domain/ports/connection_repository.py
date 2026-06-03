from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from mcpolis.adapters.repositories.connection_store import OAuthToken


class ConnectionRepository(Protocol):
    async def get_admin_token(
        self, org_id: str, upstream_id: str
    ) -> OAuthToken | None: ...

    async def put_admin_token(
        self, org_id: str, upstream_id: str, token: OAuthToken, authorized_by: str
    ) -> None: ...

    async def get_user_token(
        self, org_id: str, user_id: str, upstream_id: str
    ) -> OAuthToken | None: ...

    async def put_user_token(
        self, org_id: str, user_id: str, upstream_id: str, token: OAuthToken
    ) -> None: ...

    async def delete_user_token(
        self, org_id: str, user_id: str, upstream_id: str
    ) -> None: ...

    async def delete_all_user_tokens(self, org_id: str, user_id: str) -> int: ...

    async def delete_all_upstream_tokens(
        self, org_id: str, upstream_id: str
    ) -> int: ...

    async def get_all_stored_tokens(
        self, org_id: str
    ) -> list[tuple[str, str]]: ...

    async def get_connected_users(
        self, org_id: str, upstream_id: str
    ) -> list[str]: ...

    async def get_client_info(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> dict[str, Any] | None: ...

    async def put_client_info(
        self, org_id: str, upstream_id: str, user_id: str, client_info: dict[str, Any]
    ) -> None: ...

    async def get_connection_error(
        self, org_id: str, upstream_id: str
    ) -> str | None: ...

    async def set_connection_error(
        self,
        org_id: str,
        upstream_id: str,
        error: str,
        signature: dict[str, Any] | None = None,
    ) -> None: ...

    async def get_connection_error_signature(
        self, org_id: str, upstream_id: str
    ) -> dict[str, Any] | None: ...

    async def clear_connection_error(
        self, org_id: str, upstream_id: str
    ) -> None: ...

    async def record_refresh_failure(
        self,
        org_id: str,
        upstream_id: str,
        user_id: str,
        *,
        signature: dict[str, Any] | None = None,
    ) -> tuple[int, datetime]: ...

    async def reset_refresh_failures(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> None: ...

    async def get_refresh_failures(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> tuple[int, datetime] | None: ...

    async def get_refresh_failure_signature(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> dict[str, Any] | None: ...

    async def mark_notified(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> None: ...

    async def was_notified(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> bool: ...

    async def clear_notified(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> None: ...

    async def set_enabled(self, org_id: str, upstream_id: str) -> None:
        """Remove any explicit-disabled marker so the upstream falls
        back to the default-enabled state. Idempotent. Phase E
        bistate semantic — replaces the prior tristate where
        ``set_enabled`` wrote an ``enabled: True`` row and
        ``clear_enabled`` removed it; absence of a row is now the
        canonical "enabled" state."""
        ...

    async def set_disabled(self, org_id: str, upstream_id: str) -> None:
        """Persist an explicit ``enabled: False`` so the boot
        reconciler skips this upstream across restarts. The only
        durable state stored — absence implies enabled."""
        ...

    async def is_enabled(self, org_id: str, upstream_id: str) -> bool:
        """``True`` iff no explicit-disabled marker exists for
        ``upstream_id``."""
        ...

    async def get_disabled_ids(self, org_id: str) -> set[str]: ...

    async def put_pending_code(
        self, org_id: str, upstream_id: str, user_id: str, code: str, original_state: str
    ) -> None: ...

    async def pop_pending_code(
        self, org_id: str, upstream_id: str, user_id: str
    ) -> tuple[str, str] | None: ...
