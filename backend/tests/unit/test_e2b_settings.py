"""E2B ``Settings`` field defaults + env parsing (CFG-3).

These are the cost/UX knobs that drive ``_build_sandbox_provider_plumbing``
when it constructs the ``E2BSandboxService`` (idle-pause window, reuse-on-
restart, the Volumes account gate, and the one-shot fresh-sandboxes
override). A drift in any default silently changes the deployed cost or
recovery behaviour, so this file pins both the defaults and the
env-style coercion round-trip.

Mirrors the established pattern in ``test_settings_bool_coercion.py``:
``Settings(_env_file=None, **overrides)`` exercises the same parsing
path env vars take, deterministically and without touching the real
``.env``. One test additionally drives the genuine ``os.environ``
round-trip (set + restore in-body, no fixture) to prove the
``MCPOLIS_`` prefix wiring.
"""
from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from mcpolis.entrypoints.config import Settings


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# ---------- defaults ----------


def test_e2b_settings_defaults() -> None:
    """The shipped defaults are the cost/UX contract: a 60s idle-pause
    window, reuse-on-restart ON (sub-second wake vs ~25s cold install),
    Volumes OFF (account feature-flag gated), and the fresh-sandboxes
    one-shot override OFF."""
    settings = make_settings()
    assert settings.e2b_idle_pause_seconds == 60
    assert settings.e2b_reuse_sandboxes_on_restart is True
    assert settings.e2b_volumes_enabled is False
    assert settings.e2b_fresh_sandboxes is False


# ---------- env-style coercion round-trip (string inputs) ----------


def test_e2b_idle_pause_seconds_parses_int_string() -> None:
    """``MCPOLIS_E2B_IDLE_PAUSE_SECONDS=120`` arrives as a string from
    the environment; pydantic coerces it to the int the service
    threads into ``Sandbox.create(timeout=…)``."""
    settings = make_settings(e2b_idle_pause_seconds="120")
    assert settings.e2b_idle_pause_seconds == 120


def test_e2b_reuse_sandboxes_on_restart_parses_falsey_string() -> None:
    """Operators revert to clean-slate-per-deploy by setting the env
    var to a falsey string; it must coerce to ``False`` (not stay at
    the ``True`` default)."""
    settings = make_settings(e2b_reuse_sandboxes_on_restart="false")
    assert settings.e2b_reuse_sandboxes_on_restart is False


def test_e2b_volumes_enabled_parses_truthy_string() -> None:
    """Flipping the Volumes account gate on is a truthy-string env
    write once the E2B dashboard has Volumes enabled."""
    settings = make_settings(e2b_volumes_enabled="1")
    assert settings.e2b_volumes_enabled is True


def test_e2b_fresh_sandboxes_parses_truthy_string() -> None:
    """The one-shot 'give me a clean slate' override is a truthy-string
    env write for a single restart."""
    settings = make_settings(e2b_fresh_sandboxes="true")
    assert settings.e2b_fresh_sandboxes is True


def test_e2b_bool_fields_empty_string_coerce_to_false() -> None:
    """A stray ``MCPOLIS_E2B_VOLUMES_ENABLED=`` line (blank value) must
    read as 'declared but off' rather than crashing startup — the
    before-validator that protects every bool field covers these too."""
    settings = make_settings(
        e2b_reuse_sandboxes_on_restart="",
        e2b_volumes_enabled="",
        e2b_fresh_sandboxes="",
    )
    assert settings.e2b_reuse_sandboxes_on_restart is False
    assert settings.e2b_volumes_enabled is False
    assert settings.e2b_fresh_sandboxes is False


def test_e2b_idle_pause_seconds_rejects_garbage() -> None:
    """A non-numeric idle-pause value fails fast at startup rather than
    silently defaulting — the operator wrote something wrong."""
    with pytest.raises(ValidationError):
        make_settings(e2b_idle_pause_seconds="not-a-number")


# ---------- genuine os.environ round-trip ----------


def test_e2b_settings_read_from_environment_with_prefix() -> None:
    """End-to-end: the ``MCPOLIS_`` prefix wiring resolves real env
    vars into the e2b fields. Set + restore in-body (no fixture) so the
    test is self-contained and leaves the process environment clean."""
    keys = {
        "MCPOLIS_E2B_IDLE_PAUSE_SECONDS": "300",
        "MCPOLIS_E2B_REUSE_SANDBOXES_ON_RESTART": "false",
        "MCPOLIS_E2B_VOLUMES_ENABLED": "true",
        "MCPOLIS_E2B_FRESH_SANDBOXES": "true",
    }
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in keys}
    try:
        for k, v in keys.items():
            os.environ[k] = v
        settings = make_settings()
        assert settings.e2b_idle_pause_seconds == 300
        assert settings.e2b_reuse_sandboxes_on_restart is False
        assert settings.e2b_volumes_enabled is True
        assert settings.e2b_fresh_sandboxes is True
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
