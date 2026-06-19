"""Unit guardrails for ``BoundedLineBuffer`` (SBX-7 / BUG-6).

The shared newline-framer both sandbox backends use. These pin the
contract directly (the backend SBX-7 tests exercise it end-to-end via
tracemalloc retention): complete lines surface, partials are retained,
and a runaway newline-free line is bounded to one short marker.
"""
from __future__ import annotations

from mcpolis.domain.services.stdout_framing import (
    MAX_STDOUT_LINE_BYTES,
    BoundedLineBuffer,
)


def make_small_buffer(max_line_bytes: int = 16) -> BoundedLineBuffer:
    """A buffer with a tiny cap so overflow is cheap to exercise."""
    return BoundedLineBuffer(max_line_bytes=max_line_bytes)


def test_complete_lines_surface_in_order() -> None:
    buf = make_small_buffer()
    assert buf.feed("a\nb\nc\n") == ["a", "b", "c"]


def test_partial_line_is_retained_until_newline() -> None:
    buf = make_small_buffer()
    assert buf.feed("hel") == []
    assert buf.feed("lo\nwor") == ["hello"]
    assert buf.feed("ld\n") == ["world"]


def test_chunk_split_across_newline_reassembles() -> None:
    buf = make_small_buffer()
    assert buf.feed("ab") == []
    assert buf.feed("c\n") == ["abc"]


def test_oversized_no_newline_line_is_bounded_to_one_marker() -> None:
    """A line that crosses the cap with no newline yields a single short
    marker and then drops the rest of the runaway line — the retained
    leftover never exceeds the cap."""
    buf = make_small_buffer(max_line_bytes=16)
    first = buf.feed("x" * 64)
    assert len(first) == 1
    assert first[0].startswith("[mcpolis: dropped an oversized stdout line")
    # The marker is short and bounded — never the runaway payload.
    assert len(first[0]) < 16 + 200
    # More no-newline bytes keep being dropped, not buffered.
    assert buf.feed("y" * 64) == []
    assert buf.feed("z" * 64) == []
    # The internal leftover stays empty while discarding.
    assert buf._leftover == ""  # type: ignore[reportPrivateUsage]


def test_newline_after_overflow_resumes_framing() -> None:
    """Once a newline ends the oversized line, normal framing resumes
    from just after it (the dropped tail does not corrupt the next line)."""
    buf = make_small_buffer(max_line_bytes=16)
    over = buf.feed("x" * 64)
    assert len(over) == 1  # the marker
    # Newline terminates the dropped line; "good" frames cleanly after it.
    assert buf.feed("trailing-junk\ngood\n") == ["good"]


def test_default_cap_passes_a_normal_json_rpc_line() -> None:
    """A realistic JSON-RPC line (well under 1 MiB) is never treated as
    oversized."""
    buf = BoundedLineBuffer()
    line = '{"jsonrpc":"2.0","id":1,"result":{"x":"' + "y" * 4096 + '"}}'
    assert len(line) < MAX_STDOUT_LINE_BYTES
    assert buf.feed(line + "\n") == [line]
