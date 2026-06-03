"""``LogBuffer`` subclass that masks secret values at write time.

When a sandbox process echoes a value the gateway substituted into
its environment to stderr (e.g. a 401 response that quotes the
bearer token, or a misconfigured tool that prints its env at
startup), the operator's "Server logs" panel would otherwise show
the plaintext. We redact at write time so the in-memory ring buffer
never holds plaintext past the substitution boundary, narrowing the
leak surface to the brief window between resolve and process spawn
(documented elsewhere as the lifetime of a substituted upstream).

Scope: only ``is_secret=true`` template-variable values are
redacted. Plain (``is_secret=false``) values are operator-visible
by design — redacting them would be confusing.

Threshold: short values are skipped to limit collateral damage on
unrelated text that happens to contain the same substring (an
8-char threshold matches the heuristic E2B uses internally; the
operator can mark a short variable as plain if they care).

Replacement form: ``[REDACTED:NAME]`` so the operator can still
correlate which variable the leak surfaced from.
"""
from __future__ import annotations

from mcpolis.adapters.upstream_clients.log_buffer import (
    DEFAULT_MAX_BYTES,
    LogBuffer,
)


__all__ = ["RedactingLogBuffer", "MIN_REDACT_LEN"]


MIN_REDACT_LEN = 8


class RedactingLogBuffer(LogBuffer):
    """A :class:`LogBuffer` that masks a known set of secret strings
    as ``[REDACTED:NAME]`` before storing them.

    The redaction map is ``{secret_value: variable_name}`` and is
    refreshed at every session start by
    :meth:`UpstreamClientManager._resolve_upstream_template_vars` — the
    same code path that substitutes ``${NAME}`` references.
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        super().__init__(max_bytes=max_bytes)
        self._redactions: dict[str, str] = {}

    def set_redactions(self, redactions: dict[str, str]) -> None:
        """Replace the redaction set.

        Values shorter than :data:`MIN_REDACT_LEN` are silently
        dropped — too noisy in practice, and the operator can re-mark
        a short variable as plain if they care.
        """
        self._redactions = {
            value: name
            for value, name in redactions.items()
            if len(value) >= MIN_REDACT_LEN
        }

    def write(self, s: str) -> int:
        if self._redactions and s:
            s = self._apply_redactions(s)
        return super().write(s)

    def _apply_redactions(self, s: str) -> str:
        # Iterate longest-first so a value that contains a shorter
        # value as a suffix doesn't get the suffix redacted twice.
        for value, name in sorted(
            self._redactions.items(), key=lambda kv: -len(kv[0]),
        ):
            if value in s:
                s = s.replace(value, f"[REDACTED:{name}]")
        return s
