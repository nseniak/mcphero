"""Shared fake for the ``UpstreamClientManager`` slice the router's
stall-recovery loop touches.

Five test modules grew their own near-identical copy of this
(``test_tool_router``, ``test_resource_prompt_router``,
``test_read_prompt_retry_double_execute``, ``test_policy_notifier``,
``test_acquire_and_refresh_with_recovery``). Every time
``_dispatch_with_recovery`` reached for one more manager method, all
three broke the same way with an ``AttributeError`` that says nothing
about the behaviour under test. One fake means the next such addition
lands in one place.

Built via ``make_stall_client_manager()`` per project convention
rather than a pytest fixture — callers pass it explicitly.
"""
from __future__ import annotations

from typing import Any


class StallClientManagerFake:
    """The service_account manager surface a stall dispatch needs.

    ``ensure_shared_connected`` / ``get_session`` serve session
    resolution; ``reconnect_shared_fresh`` is the heal.
    """

    def __init__(
        self,
        session: Any = None,
        *,
        heal_error: Exception | None = None,
    ) -> None:
        self.session = session
        self._heal_error = heal_error
        self.ensure_calls = 0
        self.fresh_calls = 0
        # The ``stale`` session each heal was told had stalled, in order.
        self.stale_seen: list[Any] = []

    async def ensure_shared_connected(self, upstream: Any) -> Any:
        del upstream
        self.ensure_calls += 1
        return self.session

    def get_session(self, upstream_id: str, user_id: str | None = None) -> Any:
        del upstream_id, user_id
        return self.session

    async def reconnect_shared_fresh(
        self, upstream: Any, *, stale: Any = None,
    ) -> Any:
        del upstream
        self.stale_seen.append(stale)
        self.fresh_calls += 1
        if self._heal_error is not None:
            raise self._heal_error
        return self.session


def make_stall_client_manager(
    session: Any = None,
    *,
    heal_error: Exception | None = None,
) -> StallClientManagerFake:
    return StallClientManagerFake(session, heal_error=heal_error)
