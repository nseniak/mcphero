from __future__ import annotations

import asyncio

from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer
from mcpolis.adapters.upstream_clients.log_buffer_redacted import (
    RedactingLogBuffer,
)


__all__ = ["LogBufferRegion"]


class LogBufferRegion:
    """Owns the per-upstream stderr capture buffers.

    Carved out of ``UpstreamClientManager`` as the smallest of the
    five region classes (internal/plans/manager-region-split.md, Phase 1).
    Holds one :class:`RedactingLogBuffer` per stdio upstream id;
    entries are created lazily on first stdio session open and
    persist across reconnects so an operator can still read crash
    logs after a transport drop.

    Orthogonal to upstream-level state (sessions, transitions) and
    per-user-session state — the manager facade keeps its public
    log-reading methods as one-line delegations to this region.

    Buffers are :class:`RedactingLogBuffer` so secret template-var
    values can be masked at write time. The redaction map is
    refreshed by :meth:`UpstreamClientManager._resolve_upstream_template_vars`
    on every session start via :meth:`set_redactions`; without a
    refresh the buffer redacts nothing (empty redaction map).
    """

    def __init__(self) -> None:
        self._buffers: dict[str, RedactingLogBuffer] = {}

    def get_or_create(self, upstream_id: str) -> RedactingLogBuffer:
        """Return the buffer for ``upstream_id``, creating one bound
        to the current running loop on first access.

        Used by stdio session creation to wire stderr capture before
        the subprocess starts. Idempotent — repeated calls return
        the same buffer so logs accumulate across reconnects.
        """
        buf = self._buffers.get(upstream_id)
        if buf is None:
            buf = RedactingLogBuffer()
            buf.bind_loop(asyncio.get_event_loop())
            self._buffers[upstream_id] = buf
        return buf

    def get(self, upstream_id: str) -> LogBuffer | None:
        """Return the buffer for ``upstream_id``, or ``None`` when
        no stdio session has ever opened for that upstream."""
        return self._buffers.get(upstream_id)

    def get_output(self, upstream_id: str) -> str | None:
        """Return captured stderr output, or ``None`` when no
        buffer exists. Convenience for log-viewing UIs that do not
        need the buffer object itself."""
        buf = self._buffers.get(upstream_id)
        if buf is None:
            return None
        return buf.get_output()

    def set_redactions(
        self, upstream_id: str, redactions: dict[str, str],
    ) -> None:
        """Configure the secret-redaction set for one upstream.

        ``redactions`` maps ``{secret_value: variable_name}``. Values
        shorter than :data:`MIN_REDACT_LEN` (8 chars) are dropped
        inside the buffer.

        Idempotent: pass an empty dict to clear. Creates the buffer
        on first call so the redaction set is in place before the
        sandbox writes anything.
        """
        buf = self.get_or_create(upstream_id)
        buf.set_redactions(redactions)
