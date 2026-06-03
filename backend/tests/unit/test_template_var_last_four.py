"""Boundary tests for ``compute_last_four`` — pinned at >16 chars.

Documents the chosen threshold so a future "soften this" PR has to
consciously change the test. See plan §"Secret storage" for the
math: at 17 chars the leak ratio drops below ~24%, which is the
regime where modern API tokens live.
"""
from __future__ import annotations

from mcpolis.domain.model.template_var import LAST_FOUR_MIN_LENGTH, compute_last_four


def test_compute_last_four_threshold_is_strictly_above_16() -> None:
    assert LAST_FOUR_MIN_LENGTH == 16


def test_compute_last_four_returns_none_for_short_value() -> None:
    assert compute_last_four("short") is None


def test_compute_last_four_returns_none_at_threshold() -> None:
    # Exactly 16 chars → still no preview.
    sixteen = "x" * 16
    assert compute_last_four(sixteen) is None


def test_compute_last_four_returns_last_four_above_threshold() -> None:
    seventeen = "x" * 13 + "wXY4"
    assert len(seventeen) == 17
    assert compute_last_four(seventeen) == "wXY4"


def test_compute_last_four_on_long_token() -> None:
    token = "ghp_abcdefghijklmnop1234567890ABCDEF"
    assert compute_last_four(token) == token[-4:]
