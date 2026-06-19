"""Unit guardrails for ``confine_to_sandbox_home`` (SBX-11 / BUG-5).

The shared confinement rule both sandbox backends use for
materialize-file target paths. The backend traversal tests exercise it
end-to-end; these pin the edge cases directly.
"""
from __future__ import annotations

import os

import pytest

from mcpolis.domain.services.sandbox_path import (
    SandboxFilePathError,
    confine_to_sandbox_home,
)

HOME = "/home/user"


def test_path_inside_home_is_returned_normalized() -> None:
    assert (
        confine_to_sandbox_home("/home/user/.config/cred", HOME)
        == "/home/user/.config/cred"
    )


def test_relative_path_is_resolved_under_home() -> None:
    assert (
        confine_to_sandbox_home(".config/cred", HOME)
        == "/home/user/.config/cred"
    )


def test_home_itself_is_confined() -> None:
    assert confine_to_sandbox_home("/home/user", HOME) == "/home/user"


def test_absolute_path_outside_home_is_rejected() -> None:
    with pytest.raises(SandboxFilePathError):
        confine_to_sandbox_home("/etc/cron.d/pwned", HOME)


def test_dotdot_traversal_above_home_is_rejected() -> None:
    with pytest.raises(SandboxFilePathError):
        confine_to_sandbox_home("/home/user/../../etc/escape", HOME)


def test_relative_dotdot_traversal_is_rejected() -> None:
    with pytest.raises(SandboxFilePathError):
        confine_to_sandbox_home("../../etc/escape", HOME)


def test_sibling_prefix_is_not_treated_as_confined() -> None:
    """``/home/user-evil`` shares a string prefix with ``/home/user`` but
    is NOT under it — commonpath must reject it."""
    with pytest.raises(SandboxFilePathError):
        confine_to_sandbox_home("/home/user-evil/cred", HOME)


def test_inner_dotdot_that_stays_under_home_is_allowed() -> None:
    """A ``..`` that normalizes back to within the home is fine."""
    assert (
        confine_to_sandbox_home("/home/user/a/../b/cred", HOME)
        == os.path.normpath("/home/user/b/cred")
    )
