"""Per-upstream connection state — the single source of truth.

Replaces the prior eight legacy dicts on ``UpstreamClientManager``
(``_sessions``, ``_tasks``, ``_admin_sessions``, ``_admin_tasks``,
``_server_info``, ``_self_descriptions``, ``_deferred_attach_upstreams``,
``_background_connect_tasks``) with one ``UpstreamState`` record per
upstream id. Every reader on the manager queries this record; every
writer goes through one of the ``_transition_to_*`` methods on the
manager.

Per-user sessions, log buffers, and the lazy-connect single-flight
infrastructure stay in their own dicts on the manager — they're
keyed differently and have orthogonal lifecycles.

The five states are mutually exclusive and exhaustive: every
upstream the manager knows about is in exactly one of them. See
``internal/plans/upstream-state-machine-refactor.md`` for the full design
rationale and migration story.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from mcp.client.session import ClientSession

from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamSelfDescription,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from mcpolis.adapters.upstream_clients.client_manager import (
        ConnectionTask,
    )


class UpstreamConnectionState(StrEnum):
    """Lifecycle phase of an upstream from the manager's POV.

    Mutually exclusive — every upstream in ``_state`` is in exactly
    one of these:

    - ``DISABLED``: admin Stopped or auto-disabled after a connect
      failure. ``last_failure`` may be set when auto-disabled.
    - ``DEFERRED_ATTACH``: persistence carries cached metadata; no
      live session. ``ensure_shared_connected`` reattaches lazily on
      the first tool dispatch. From the user's POV the upstream is
      Ready — the cache satisfies dashboard reads.
    - ``CONNECTING``: an admin clicked Start/Reconnect and the
      fire-and-forget reconnect task is in flight. Drives the
      dashboard's "Starting…" disabled-button state.
    - ``LIVE``: at least one usable upstream-level session exists
      (shared discovery and/or admin OAuth). End users can dispatch
      tool calls (directly for service_account / shared OAuth, or
      after personal authentication for per_user_oauth).
    - ``FAILED``: no working connection. Either a connect attempt
      failed (``last_failure`` set) or the upstream was registered
      but never connected (``last_failure`` is ``None``).
    """

    DISABLED = "disabled"
    DEFERRED_ATTACH = "deferred_attach"
    CONNECTING = "connecting"
    LIVE = "live"
    FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class UpstreamState:
    """Per-upstream connection state record.

    Slot semantics (invariants enforced by ``_transition_to_*``):

    - ``state == LIVE`` ⇒ at least one of ``shared_session`` /
      ``admin_session`` is set.
    - ``state == DEFERRED_ATTACH`` ⇒ ``server_info`` AND
      ``self_description`` are set; no live sessions.
    - ``state == CONNECTING`` ⇒ ``background_task`` is set and not
      done. Cached metadata (if any) is preserved so dashboard
      reads still resolve while reconnecting.
    - ``state == DISABLED`` ⇒ no live sessions. ``last_failure`` is
      set when DISABLED came from auto-disable-on-failure, ``None``
      when DISABLED is from an explicit admin Stop.
    - ``state == FAILED`` ⇒ no live sessions.

    Both ``shared_session`` and ``admin_session`` slots can be live
    simultaneously for OAuth upstreams: discovery shared connect
    populates shared_session; admin OAuth populates admin_session.
    Closing one doesn't drop the other.
    """

    state: UpstreamConnectionState
    shared_session: ClientSession | None = None
    shared_task: ConnectionTask | None = None
    admin_session: ClientSession | None = None
    admin_task: ConnectionTask | None = None
    server_info: ServerInfo | None = None
    self_description: UpstreamSelfDescription | None = None
    background_task: asyncio.Task[None] | None = None
    last_failure: str | None = None
    last_transition_at: datetime = field(default_factory=_utcnow)
    # SHA-256 fingerprint of the persisted config + env-var set that
    # this LIVE / CONNECTING / DEFERRED_ATTACH session was started
    # with. ``None`` while DISABLED / FAILED. Drives the detail-page
    # "stop & restart" dirty banner: if the live config hash diverges
    # from this snapshot, the user has saved changes the running
    # session can't see.
    started_config_hash: str | None = None

    @property
    def has_any_session(self) -> bool:
        return self.shared_session is not None or self.admin_session is not None
