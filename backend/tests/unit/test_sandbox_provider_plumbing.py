"""``_build_sandbox_provider_plumbing`` startup-warning behavior.

The README promises stdio MCPs running as local subprocesses are
"flagged as unsafe at startup". This guards that the warning actually
fires when the provider resolves to ``local-subprocess`` and stays
silent when an isolated backend (E2B) is selected.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import cast

import structlog

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.entrypoints.app import _build_sandbox_provider_plumbing
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.storage_factory import StorageBundle

_WARN_EVENT = "sandbox.provider.local_subprocess.unsafe"


def _fake_storage() -> StorageBundle:
    # The plumbing builder only reads ``sandbox_persistence_repo``.
    return cast(
        StorageBundle,
        SimpleNamespace(
            sandbox_persistence_repo=InMemorySandboxPersistenceRepository(),
        ),
    )


def _warn_events(
    logs: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    return [e for e in logs if e.get("event") == _WARN_EVENT]


def test_local_subprocess_provider_warns_at_startup() -> None:
    settings = Settings(sandbox_provider="local-subprocess", e2b_api_key="")
    with structlog.testing.capture_logs() as logs:
        _build_sandbox_provider_plumbing(settings, _fake_storage())
    events = _warn_events(logs)
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"


def test_empty_provider_without_key_falls_back_and_warns() -> None:
    # Empty MCPOLIS_SANDBOX_PROVIDER + no E2B key resolves to
    # local-subprocess (the standalone Docker default) — still unsafe.
    settings = Settings(sandbox_provider="", e2b_api_key="")
    with structlog.testing.capture_logs() as logs:
        _build_sandbox_provider_plumbing(settings, _fake_storage())
    assert len(_warn_events(logs)) == 1


def test_e2b_provider_does_not_warn() -> None:
    settings = Settings(sandbox_provider="e2b", e2b_api_key="e2b_test_key")
    with structlog.testing.capture_logs() as logs:
        _build_sandbox_provider_plumbing(settings, _fake_storage())
    assert _warn_events(logs) == []
