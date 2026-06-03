"""Contract: pause/resume reflects the declared capability.

``pause()`` always returns ``None`` for an unknown ``session_id``
(no live session was registered under that id); a backend's
``supports_pause_resume`` capability is exercised end-to-end in its
backend-specific test module where the test can open a session
first. The cross-backend contract here is the no-live-session
behaviour, which must be uniform.
"""
from __future__ import annotations

import pytest

from mcpolis.domain.services.sandbox_service import SandboxService

from tests.unit.sandbox_service_contract.backends import iter_backends


@pytest.mark.asyncio
@pytest.mark.parametrize("service", iter_backends())
async def test_pause_unknown_session_returns_none(
    service: SandboxService,
) -> None:
    """Whether the backend supports pause/resume or not, pausing an
    unknown session id is a no-op that returns ``None``. Catches a
    backend that would crash or raise on a stale id (e.g. the idle
    reaper firing after the session already exited)."""
    result = await service.pause(session_id="unknown-session-id")
    assert result is None
