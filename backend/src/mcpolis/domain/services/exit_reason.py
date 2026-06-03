"""Provider-agnostic ``ExitReason`` enum.

Originally lived in ``adapters/sandbox_runner/protocol.py`` alongside
the wire-protocol codec; salvaged here when the own-runner adapter
was deleted (Phase 5 of plan ``serene-beaming-tulip.md``). The enum
is consumed by every :class:`SandboxService` impl's ``map_exit``
method, so it has to outlive the runner-specific code.

Unknown values map to ``INTERNAL_ERROR`` per the original §4.4 spec.
Backends with sparse signal (E2B) lean on ``PROVIDER_ERROR`` plus
``Exit.detail`` for the operator-facing message.
"""
from __future__ import annotations

from enum import StrEnum


class ExitReason(StrEnum):
    NOT_IMPLEMENTED = "not_implemented"
    HANDSHAKE_INVALID = "handshake_invalid"
    VERSION_UNSUPPORTED = "version_unsupported"
    AUTH_FAILED = "auth_failed"
    IDLE_KILLED = "idle_killed"
    OOM_KILLED = "oom_killed"
    PID_LIMIT_HIT = "pid_limit_hit"
    IMAGE_PULL_FAILED = "image_pull_failed"
    EGRESS_POLICY_DENIED = "egress_policy_denied"
    KILL_SWITCH = "kill_switch"
    RUNNER_SHUTDOWN = "runner_shutdown"
    SUBPROCESS_EXITED = "subprocess_exited"
    INTERNAL_ERROR = "internal_error"
    # Catch-all for provider-side failures the backend can't categorize
    # into one of the specific reasons above. Pair with ``Exit.detail``
    # so the admin UI surfaces the raw provider message verbatim.
    PROVIDER_ERROR = "provider_error"
    # Provider-side resource cap exceeded (account quota, billing cap,
    # plan limit). Distinct from ``AUTH_FAILED`` (bad credential) — the
    # credential is good, the account just can't spawn another sandbox.
    ACCOUNT_LIMIT_EXCEEDED = "account_limit_exceeded"


__all__ = ["ExitReason"]
