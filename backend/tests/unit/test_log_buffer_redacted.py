"""``RedactingLogBuffer`` unit coverage.

The contract: secret values configured via ``set_redactions`` are
replaced with ``[REDACTED:NAME]`` before they hit the parent ring
buffer. Plain values pass through; values shorter than
``MIN_REDACT_LEN`` are skipped (collateral-damage trade-off).
"""
from __future__ import annotations

import pytest

from mcpolis.adapters.upstream_clients.log_buffer_redacted import (
    MIN_REDACT_LEN,
    RedactingLogBuffer,
)


def test_secret_value_is_redacted_with_variable_name() -> None:
    buf = RedactingLogBuffer()
    buf.set_redactions({"ghp_supersecretvalue123": "GITHUB_TOKEN"})
    buf.write("Bearer ghp_supersecretvalue123 was rejected")
    output = buf.get_output()
    assert "[REDACTED:GITHUB_TOKEN]" in output
    assert "ghp_supersecretvalue123" not in output


def test_no_redaction_when_set_empty() -> None:
    buf = RedactingLogBuffer()
    buf.write("anything secret-looking like ghp_supersecret")
    assert "ghp_supersecret" in buf.get_output()


def test_short_values_are_skipped() -> None:
    """``MIN_REDACT_LEN`` keeps the redactor from chewing up
    legitimate words that happen to match a ridiculously short
    'secret'. Operators with short values can mark them as plain."""
    short_value = "x" * (MIN_REDACT_LEN - 1)
    buf = RedactingLogBuffer()
    buf.set_redactions({short_value: "SHORT"})
    buf.write(f"saw {short_value} in the wild")
    assert short_value in buf.get_output()
    assert "[REDACTED:SHORT]" not in buf.get_output()


def test_multiple_redactions_applied_longest_first() -> None:
    """A long value that contains a shorter value's bytes as a
    suffix must redact as the long value, not the short one."""
    buf = RedactingLogBuffer()
    buf.set_redactions(
        {
            "abcdefgh": "SHORT",      # 8 chars, qualifies
            "abcdefghextra": "LONG",  # contains SHORT as prefix
        },
    )
    buf.write("payload abcdefghextra trailing")
    output = buf.get_output()
    assert "[REDACTED:LONG]" in output
    assert "[REDACTED:SHORT]" not in output
    assert "abcdefgh" not in output


def test_redaction_survives_multi_line_writes() -> None:
    buf = RedactingLogBuffer()
    buf.set_redactions({"ghp_supersecretvalue": "TOKEN"})
    buf.write("line 1: ghp_supersecretvalue\nline 2: more text\n")
    output = buf.get_output()
    assert "ghp_supersecretvalue" not in output
    assert "[REDACTED:TOKEN]" in output
    # The base buffer's per-line timestamp wrap still happens after
    # redaction, so the redacted token is on its own line as expected.
    assert "line 2: more text" in output


def test_set_redactions_replaces_previous_set() -> None:
    """``set_redactions`` is documented as a full replace, not a
    merge — fits the per-session refresh contract."""
    buf = RedactingLogBuffer()
    buf.set_redactions({"old_secret_value": "OLD"})
    buf.set_redactions({"new_secret_value": "NEW"})
    buf.write("old_secret_value new_secret_value")
    output = buf.get_output()
    assert "old_secret_value" in output  # not in the new set
    assert "new_secret_value" not in output
    assert "[REDACTED:NEW]" in output


def test_unknown_value_passes_through() -> None:
    buf = RedactingLogBuffer()
    buf.set_redactions({"known_secret_long_enough": "KNOWN"})
    buf.write("totally unrelated content")
    assert "totally unrelated content" in buf.get_output()


def test_empty_write_is_a_no_op() -> None:
    buf = RedactingLogBuffer()
    buf.set_redactions({"secretvalue123": "X"})
    assert buf.write("") == 0
    assert buf.get_output() == ""


@pytest.mark.parametrize(
    "value, name",
    [
        ("longenoughtoken", "ANYTHING"),
        ("a" * MIN_REDACT_LEN, "EXACTLY_AT_THRESHOLD"),
    ],
)
def test_redactions_at_or_above_min_length(value: str, name: str) -> None:
    buf = RedactingLogBuffer()
    buf.set_redactions({value: name})
    buf.write(f"prefix {value} suffix")
    output = buf.get_output()
    assert value not in output
    assert f"[REDACTED:{name}]" in output
