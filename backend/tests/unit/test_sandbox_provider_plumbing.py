"""``_build_sandbox_provider_plumbing`` startup-warning behavior +
explicit-provider wiring / error paths (CFG-2).

The README promises stdio MCPs running as local subprocesses are
"flagged as unsafe at startup". This guards that the warning actually
fires when the provider resolves to ``local-subprocess`` and stays
silent when an isolated backend (E2B) is selected.

The CFG-2 block additionally pins that the explicit ``e2b`` provider
builds an ``E2BSandboxService`` wired with the cost/UX settings
(``volumes_enabled`` / ``on_timeout_seconds`` /
``reuse_sandboxes_on_restart``), that a provider the builder can't
construct raises ``RuntimeError``, and that the empty-provider +
key branch resolves to ``e2b``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import cast

import pytest
import structlog

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import E2BSandboxService
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


# ---------- CFG-2: explicit-provider wiring + error paths ----------


def test_explicit_e2b_builds_service_wired_from_settings() -> None:
    """An explicit ``e2b`` provider + key builds an ``E2BSandboxService``
    in the registry, wired with the operator's cost/UX settings:
    ``volumes_enabled``, ``on_timeout_seconds`` (the idle-pause window),
    and ``reuse_sandboxes_on_restart``. A drift here silently changes
    deployed cost or recovery behaviour, so pin the threading
    end-to-end."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        sandbox_provider="e2b",
        e2b_api_key="e2b_test_key",
        e2b_volumes_enabled="true",
        e2b_idle_pause_seconds="123",
        e2b_reuse_sandboxes_on_restart="true",
    )
    resolver, services, persistence, instance_id = (
        _build_sandbox_provider_plumbing(settings, _fake_storage())
    )

    assert "e2b" in services
    e2b_service = services["e2b"]
    assert isinstance(e2b_service, E2BSandboxService)
    # The resolver points at the explicit provider.
    assert asyncio.run(resolver.resolve(org_id="acme")) == "e2b"
    # Settings threaded through onto the service.
    assert e2b_service._volumes_enabled is True  # type: ignore[reportPrivateUsage]
    assert e2b_service._on_timeout_seconds == 123  # type: ignore[reportPrivateUsage]
    # ``reuse_sandboxes_on_restart`` is gated on a persistence repo
    # being present; the fake storage supplies one, so the effective
    # flag is True.
    assert e2b_service._reuse_sandboxes_on_restart is True  # type: ignore[reportPrivateUsage]
    # Capabilities reflect the wiring (volumes on + persistence present).
    assert e2b_service.capabilities().supports_persistent_disk is True
    # The shared persistence + a minted instance id come back too.
    assert persistence is not None
    assert instance_id


def test_provider_not_buildable_raises_runtime_error() -> None:
    """An explicit provider the builder can't construct from current
    settings (here: ``e2b`` requested but no API key, so it never
    landed in the registry) is a misconfiguration — raise
    ``RuntimeError`` rather than silently falling back to the unsafe
    local path."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        sandbox_provider="e2b",
        e2b_api_key="",
    )
    with pytest.raises(RuntimeError) as exc:
        _build_sandbox_provider_plumbing(settings, _fake_storage())
    assert "e2b" in str(exc.value)


def test_empty_provider_with_key_resolves_to_e2b() -> None:
    """Empty ``MCPOLIS_SANDBOX_PROVIDER`` + an API key falls back to the
    ``e2b`` branch (not the unsafe local-subprocess default) — and
    therefore emits no unsafe warning."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        sandbox_provider="",
        e2b_api_key="e2b_test_key",
    )
    with structlog.testing.capture_logs() as logs:
        resolver, services, _persistence, _instance_id = (
            _build_sandbox_provider_plumbing(settings, _fake_storage())
        )
    assert asyncio.run(resolver.resolve(org_id="acme")) == "e2b"
    assert isinstance(services["e2b"], E2BSandboxService)
    assert _warn_events(logs) == []
