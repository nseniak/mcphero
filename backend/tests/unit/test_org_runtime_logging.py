"""Tests for the startup/runtime logs emitted by ``OrgRuntimeManager``.

Specifically pins the ``org.runtime.initialized`` event, which is the
first thing a prod-log reader sees when investigating connection or
auth anomalies. In the past this event only surfaced upstream and role
counts — opaque about *who* the admin actually is — which led directly
to a misdiagnosis ("the UI is wishful thinking about a non-admin's
Notion sign-in") that turned out to be the result of guessing the
admin's email. Exposing ``admin_emails`` as a structured field makes
the "who is admin?" question unambiguous from the log alone and
alertable in the log backend.
"""
from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import MagicMock

import structlog
from structlog.types import EventDict

from mcpolis.domain.model.settings import (
    RoleDefinition,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.services.org_runtime import OrgRuntimeManager


def _make_manager() -> OrgRuntimeManager:
    """Pass mocks for every repo — ``_build_runtime`` is sync and
    only constructs in-memory objects; no repo call is made."""
    return OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )


def _make_config(
    users: dict[str, str],
    admin_role_name: str = "admin",
    user_role_name: str = "user",
) -> SettingsConfig:
    """Build a ``SettingsConfig`` with an admin role + non-admin role
    and a ``users`` dict mapping email → role name."""
    return SettingsConfig(
        roles={
            admin_role_name: RoleDefinition(is_admin=True, is_default=False),
            user_role_name: RoleDefinition(is_admin=False, is_default=True),
        },
        users={
            email: UserDefinition(role=role) for email, role in users.items()
        },
    )


def _find_init_event(logs: Sequence[EventDict]) -> EventDict:
    """Return the single ``org.runtime.initialized`` event from ``logs``.

    Fails the caller via ``assert`` if zero or more than one are found —
    both shapes indicate a regression worth surfacing explicitly rather
    than silently selecting the first match.
    """
    matches = [e for e in logs if e.get("event") == "org.runtime.initialized"]
    assert len(matches) == 1, (
        f"expected exactly one org.runtime.initialized event, got {matches}"
    )
    return matches[0]


def test_runtime_event_names_single_admin() -> None:
    """The ``org.runtime.initialized`` event must surface the admin email
    as a structured field so a prod-log reader doesn't have to correlate
    against Mongo."""
    manager = _make_manager()
    config = _make_config({
        "admin@example.com": "admin",
        "alice@example.com": "user",
    })

    with structlog.testing.capture_logs() as logs:
        manager._build_runtime("org-1", config, upstreams=[])  # pyright: ignore[reportPrivateUsage]

    event = _find_init_event(logs)
    assert event["org_id"] == "org-1"
    assert event["admin_emails"] == ["admin@example.com"]
    assert event["non_admin_user_count"] == 1


def test_runtime_event_handles_multiple_admins() -> None:
    manager = _make_manager()
    config = _make_config({
        "a1@example.com": "admin",
        "a2@example.com": "admin",
        "alice@example.com": "user",
        "bob@example.com": "user",
    })

    with structlog.testing.capture_logs() as logs:
        manager._build_runtime("org-1", config, upstreams=[])  # pyright: ignore[reportPrivateUsage]

    event = _find_init_event(logs)
    # Admins listed in sorted order for stable log output.
    assert event["admin_emails"] == ["a1@example.com", "a2@example.com"]
    assert event["non_admin_user_count"] == 2


def test_runtime_event_with_zero_non_admin_users() -> None:
    """Fresh org with only a single admin configured yet. The event
    should report ``non_admin_user_count=0``, not crash on an empty
    roster."""
    manager = _make_manager()
    config = _make_config({"founder@example.com": "admin"})

    with structlog.testing.capture_logs() as logs:
        manager._build_runtime("org-1", config, upstreams=[])  # pyright: ignore[reportPrivateUsage]

    event = _find_init_event(logs)
    assert event["admin_emails"] == ["founder@example.com"]
    assert event["non_admin_user_count"] == 0


def test_runtime_event_with_empty_users_dict() -> None:
    """A freshly-seeded org where ``config.users`` is empty must not
    crash the log — ``admin_emails=[]`` is the acceptable shape."""
    manager = _make_manager()
    config = SettingsConfig(
        roles={
            "admin": RoleDefinition(is_admin=True, is_default=False),
            "user": RoleDefinition(is_admin=False, is_default=True),
        },
        users={},
    )

    with structlog.testing.capture_logs() as logs:
        manager._build_runtime("org-1", config, upstreams=[])  # pyright: ignore[reportPrivateUsage]

    event = _find_init_event(logs)
    assert event["admin_emails"] == []
    assert event["non_admin_user_count"] == 0
