"""Backend registry for the parameterized contract suite.

Tests in this directory call :func:`iter_backends` /
:func:`iter_session_backends` and parameterize over the returned list.
Every entry is a live backend: the local-subprocess service, the E2B
service over the mock SDK client, and (for the session scenarios) the
in-memory :class:`FakeSandboxService`. Adding a new backend is a single
``_live(...)`` line — the relevant contract scenarios then run against
it with no further wiring.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _pytest.mark.structures import ParameterSet

from mcpolis.adapters.sandbox_e2b import E2BSandboxService
from mcpolis.adapters.sandbox_services import LocalSubprocessSandboxService

from tests.unit.fake_sandbox_service import make_fake_sandbox_service
from tests.unit.sandbox_e2b_mock import make_mock_e2b_client

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from mcpolis.domain.services.sandbox_service import SandboxService


def _live(service: "SandboxService") -> ParameterSet:
    return pytest.param(service, id=service.name)


def iter_backends() -> list[ParameterSet]:
    """Backends suitable for *pure* contract scenarios (capabilities,
    validate_resources, map_exit, pause).
    """
    return [
        _live(LocalSubprocessSandboxService()),
        _live(
            E2BSandboxService(
                make_mock_e2b_client(),
                mcpolis_instance="contract-instance",
                on_timeout_seconds=60,
            ),
        ),
    ]


def iter_session_backends() -> list[ParameterSet]:
    """Backends with a reachable transport in the test environment.

    Used by contract scenarios that actually open ``session()``.

    The ``e2b`` slot is the in-memory :class:`FakeSandboxService`
    (``name = "e2b"``): it implements the full Protocol and its
    ``session()`` runs a real in-process MCP server over the same
    memory-stream pair shape, so the session-lifecycle contract runs
    against an E2B-shaped backend with no live E2B account. The real
    E2B SDK path is covered separately in test_e2b_sandbox_service.py.
    """

    return [
        _live(LocalSubprocessSandboxService()),
        _live(make_fake_sandbox_service()),
    ]


__all__ = ["iter_backends", "iter_session_backends"]
