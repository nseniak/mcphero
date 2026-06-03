"""Heuristic detection of raw credentials in upstream env / headers.

Defensive: callers run the scan on save and emit a structured
``secret_in_json_detected`` log event when something looks suspicious.
We never reject — the user may have a legitimate reason — and we never
log the value itself, only enough context for an admin to find the
problem (upstream id, field, key, which pattern matched, and a short
prefix preview of the offending value).

The patterns mirror the client-side scanner at
``frontend/src/lib/secret-detection.ts``. Keep the two in sync — drift
shows up as a value that fires only on one side.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Final, Literal

from pydantic import BaseModel

from mcpolis.domain.services.template_var_substitution import has_placeholder

ScanField = Literal["env", "headers"]


class ScanFinding(BaseModel):
    """One suspicious value found in env / headers."""

    field: ScanField
    key: str
    pattern: str
    # Short prefix so the user can recognise *which* value triggered
    # the warning without us echoing the full secret. Always 6 chars +
    # ellipsis when the value is longer than 6.
    match_preview: str


# Provider-specific token shapes. Anchored loosely (not ``^`` /``$``)
# so we still catch tokens embedded in larger strings (e.g. inside a
# ``Bearer ghp_...`` header value).
_PROVIDER_PATTERNS: Final[list[tuple[str, re.Pattern[str]]]] = [
    ("github_token", re.compile(r"\bgh[psoru]_[A-Za-z0-9]{16,}\b")),
    ("openai_or_stripe_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("stripe_live_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"
        ),
    ),
]

# Key-name heuristic: when the JSON key looks like a secret holder, we
# also apply the entropy fallback. Without this gate, the entropy
# check fires on hashes, opaque IDs, etc.
_SECRET_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(token|secret|key|password|auth|bearer|credential|api[_-]?key)"
)

# Entropy threshold for the heuristic fallback. 4.0 bits/char is a
# reasonable cut for "looks like a credential, not English text" —
# random base64/hex tokens hit ~5 bits/char; well-formed sentences sit
# around 3.5.
_MIN_ENTROPY_BITS: Final[float] = 4.0
_MIN_ENTROPY_LENGTH: Final[int] = 16


def _shannon_entropy(value: str) -> float:
    """Bits-per-character Shannon entropy."""
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )


def _build_preview(value: str) -> str:
    """First 6 chars + ``…`` when longer; whole value otherwise."""
    if len(value) <= 6:
        return value
    return value[:6] + "…"


def _scan_value(field: ScanField, key: str, value: str) -> ScanFinding | None:
    """First pattern that matches, or ``None``.

    Order matters: provider regexes win over the entropy heuristic so
    the more-specific reason gets reported.
    """
    if has_placeholder(value):
        # Already a ``${VAR}`` reference — nothing to flag.
        return None
    for pattern_name, regex in _PROVIDER_PATTERNS:
        if regex.search(value):
            return ScanFinding(
                field=field, key=key, pattern=pattern_name,
                match_preview=_build_preview(value),
            )
    if (
        _SECRET_KEY_RE.search(key)
        and len(value) >= _MIN_ENTROPY_LENGTH
        and _shannon_entropy(value) >= _MIN_ENTROPY_BITS
    ):
        return ScanFinding(
            field=field, key=key, pattern="high_entropy",
            match_preview=_build_preview(value),
        )
    return None


def scan_for_secrets(
    *,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> list[ScanFinding]:
    """Walk env + headers, return one finding per suspect value."""
    findings: list[ScanFinding] = []
    for key, value in (env or {}).items():
        finding = _scan_value("env", key, value)
        if finding is not None:
            findings.append(finding)
    for key, value in (headers or {}).items():
        finding = _scan_value("headers", key, value)
        if finding is not None:
            findings.append(finding)
    return findings
