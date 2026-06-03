"""Empty-string env values for bool ``Settings`` fields must coerce to False.

Pydantic's strict bool parser rejects ``""`` outright. Without the
before-validator on ``Settings``, a stray ``MCPOLIS_DEMO_SEED=`` line
in ``.env.cloud.docker.prod`` crashes the backend on startup mid-
secrets-rotation. These tests pin that operators can write blank for
"declared but off" without bricking the process.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcpolis.entrypoints.config import Settings


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_empty_string_env_for_bool_field_coerces_to_false() -> None:
    settings = make_settings(demo_mount="", demo_seed="", allow_stdio_mcp="")
    assert settings.demo_mount is False
    assert settings.demo_seed is False
    assert settings.allow_stdio_mcp is False


def test_real_truthy_strings_still_parse() -> None:
    settings = make_settings(demo_mount="1", demo_seed="true")
    assert settings.demo_mount is True
    assert settings.demo_seed is True


def test_garbage_bool_value_still_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(demo_mount="not-a-bool")


def test_empty_string_does_not_clobber_non_bool_field() -> None:
    settings = make_settings(google_client_id="")
    assert settings.google_client_id == ""
