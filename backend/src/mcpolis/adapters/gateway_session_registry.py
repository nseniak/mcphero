"""Registry mapping MCP gateway session IDs to (org, user) identities."""
from __future__ import annotations

from collections.abc import Callable

import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Callback signature: (session_id, org_id, user_id) -> None
OnSessionEvent = Callable[[str, str, str], None]


class GatewaySessionRegistry:
    """Tracks which (org, user) owns each MCP gateway session.

    Populated by the user-identity middleware in app.py on every request.
    Used by PolicyNotifier to target tool-list-change notifications.
    """

    def __init__(self) -> None:
        self._session_owner: dict[str, tuple[str, str]] = {}
        self._user_to_sessions: dict[tuple[str, str], set[str]] = {}
        self._on_disconnect: OnSessionEvent | None = None

    def set_on_disconnect(self, callback: OnSessionEvent) -> None:
        """Set a callback to be invoked when a session is unregistered."""
        self._on_disconnect = callback

    def register(self, session_id: str, org_id: str, user_id: str) -> None:
        """Record (or update) the (org, user) that owns a session."""
        prev_owner = self._session_owner.get(session_id)
        if prev_owner == (org_id, user_id):
            return
        if prev_owner is not None:
            self._user_to_sessions.get(prev_owner, set()).discard(session_id)
        self._session_owner[session_id] = (org_id, user_id)
        self._user_to_sessions.setdefault((org_id, user_id), set()).add(session_id)

    def has_session(self, session_id: str) -> bool:
        """Check if a session is already registered."""
        return session_id in self._session_owner

    def unregister(self, session_id: str) -> None:
        """Remove a session from the registry."""
        owner = self._session_owner.pop(session_id, None)
        if owner is not None:
            org_id, user_id = owner
            sessions = self._user_to_sessions.get(owner)
            if sessions is not None:
                sessions.discard(session_id)
                if not sessions:
                    del self._user_to_sessions[owner]
            if self._on_disconnect is not None:
                self._on_disconnect(session_id, org_id, user_id)

    def get_session_ids_for_user(
        self, org_id: str, user_id: str
    ) -> list[str]:
        """Return all session IDs for a given (org, user).

        Also returns sessions a user has against the multi-org cloud
        gateway (registered under ``MULTI_ORG_SENTINEL``). A policy
        change scoped to one of the user's orgs needs to flush the
        aggregated tool list those sessions advertise — without the
        sentinel sweep, role-scoped notifications never reach
        ``/mcp``-mounted clients.
        """
        from mcpolis.domain.ports import MULTI_ORG_SENTINEL

        result: list[str] = list(
            self._user_to_sessions.get((org_id, user_id), ()),
        )
        if org_id != MULTI_ORG_SENTINEL:
            result.extend(
                self._user_to_sessions.get(
                    (MULTI_ORG_SENTINEL, user_id), (),
                ),
            )
        return result

    def get_session_ids_for_org(
        self,
        org_id: str,
        user_ids_in_org: set[str] | None = None,
    ) -> list[str]:
        """Return all session IDs belonging to the given org.

        Includes:
        * sessions registered with this exact ``org_id`` (admin MCP
          mounted at ``/admin-mcp/{slug}/``);
        * sessions a member of this org has against the multi-org
          cloud gateway (registered under ``MULTI_ORG_SENTINEL``),
          when the caller passes ``user_ids_in_org`` so we can decide
          which sentinel-bound sessions belong to this org's
          membership without enumerating every cloud session.

        Without the sentinel sweep, an upstream-side
        ``tools/list_changed`` for org A never reaches a user who's
        connected via ``/mcp`` — even though their aggregated tool
        list now stale-includes A's old set.
        """
        from mcpolis.domain.ports import MULTI_ORG_SENTINEL

        result: list[str] = []
        for (owner_org, owner_user), sessions in self._user_to_sessions.items():
            if owner_org == org_id:
                result.extend(sessions)
            elif (
                owner_org == MULTI_ORG_SENTINEL
                and user_ids_in_org is not None
                and owner_user in user_ids_in_org
            ):
                result.extend(sessions)
        return result

    def get_all_session_ids(self) -> list[str]:
        """Return all tracked session IDs."""
        return list(self._session_owner)

    def get_session_owner(
        self, session_id: str
    ) -> tuple[str, str] | None:
        """Return (org_id, user_id) for a session, or None."""
        return self._session_owner.get(session_id)
