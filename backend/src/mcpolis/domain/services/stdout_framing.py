"""Bounded newline-framing of an MCP subprocess's stdout stream.

Both sandbox backends (E2B and local-subprocess) re-assemble newline-framed
JSON-RPC messages from a chunked stdout stream. A naive ``buffer += text``
keeps the leftover (the bytes after the last newline) without limit, so a
hostile or buggy MCP that emits a very long — or never newline-terminated —
line grows per-session memory without bound (SBX-7 / BUG-6).

``BoundedLineBuffer`` caps that leftover: once an in-progress line crosses
``max_line_bytes`` with no newline, it emits a single short marker line (so
the operator still sees *something* happened) and DISCARDS the rest of that
line until the next newline re-frames the stream. Memory stays bounded and
the marker — not the multi-megabyte payload — is what reaches the errlog.
"""
from __future__ import annotations

# Default cap on the in-progress (un-newline-terminated) leftover. Generous
# enough for any legitimate JSON-RPC line yet small enough that a hostile
# newline-free stream can't exhaust per-session memory.
MAX_STDOUT_LINE_BYTES = 1024 * 1024  # 1 MiB

# How much of an oversized line to quote in the dropped-line marker.
_OVERFLOW_PREVIEW_CHARS = 120


class BoundedLineBuffer:
    """Reassemble newline-framed lines from a chunked text stream with a
    bounded leftover.

    Call :meth:`feed` with each decoded chunk; it returns the complete
    lines now available (the trailing partial line is retained for the next
    call). When the retained partial exceeds ``max_line_bytes`` with no
    newline, ``feed`` returns one short ``[mcpolis: dropped an oversized
    stdout line …]`` marker in place of the runaway line and drops bytes
    until the next newline — so the caller's existing non-JSON / errlog
    handling sees a small marker, never the unbounded payload.
    """

    def __init__(self, max_line_bytes: int = MAX_STDOUT_LINE_BYTES) -> None:
        self._leftover = ""
        self._max = max_line_bytes
        # True while dropping the tail of an oversized line until the next
        # newline re-frames the stream.
        self._discarding = False

    def feed(self, text: str) -> list[str]:
        """Append *text*; return the complete lines it completes."""
        if self._discarding:
            newline_at = text.find("\n")
            if newline_at == -1:
                # Still inside the oversized line — drop the whole chunk
                # without buffering it.
                return []
            # The newline ends the oversized line; resume normal framing
            # from just after it.
            self._discarding = False
            text = text[newline_at + 1:]

        combined = self._leftover + text
        *complete_lines, leftover = combined.split("\n")
        if len(leftover) > self._max:
            complete_lines.append(self._overflow_marker(leftover))
            self._leftover = ""
            self._discarding = True
        else:
            self._leftover = leftover
        return complete_lines

    def _overflow_marker(self, leftover: str) -> str:
        preview = leftover[:_OVERFLOW_PREVIEW_CHARS]
        return (
            "[mcpolis: dropped an oversized stdout line "
            f"(> {self._max} bytes, no newline) — preview: {preview!r}]"
        )


__all__ = ["BoundedLineBuffer", "MAX_STDOUT_LINE_BYTES"]
