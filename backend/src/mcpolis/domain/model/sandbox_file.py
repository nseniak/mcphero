"""Per-MCP **Sandbox files** — operator-uploaded credential / config
files that the launcher writes into the sandbox at a configured path
before the MCP process starts.

Files solve "the SDK reads a *file* on disk" — GCP service-account
JSON, kubeconfig, AWS shared credentials, custom YAML configs. The
operator uploads the contents and picks a ``target_path``; the
launcher writes the bytes there with mode 0600 before exec.

File names do NOT participate in ``${...}`` substitution — they
live in their own namespace, separate from user Variables and
system Variables. The canonical "file on disk + env var pointing
at it" recipe (e.g. ``GOOGLE_APPLICATION_CREDENTIALS``) is wired
by typing the same ``${HOME}/...`` path on both sides — once in
the file's ``target_path`` and once in the Variable's value —
rather than via a ``${FILE_NAME}`` token. This keeps a single
mental model for what ``${X}`` means everywhere it appears.

``target_path`` itself accepts the same ``${...}`` substitutions
that env-var values accept (system + user Variables). Cycles are
structurally impossible: files don't export symbols, so user vars
can reference system vars but not files, and target_paths can
reference (system + user) vars but not other files.

Two name fields: ``name`` is the URL-safe storage key (the
``{name}`` path component on
``PUT /api/admin/upstreams/{id}/sandbox-files/{name}`` and the
listing's row id). ``display_name`` is the free-form human label
the operator types — defaults to the uploaded filename if not
supplied. The dashboard surfaces only the display name; ``name``
is an opaque slug the operator rarely sees.

Storage:
- Cloud mode encrypts ``contents`` at rest in Mongo via the same
  :class:`FieldEncryptor` the template-var repo uses.
- Standalone mode writes plaintext alongside ``mcp.json`` (the user
  runs MCP Hero on their own machine; encryption with a key on the
  same disk is security theatre).
- ``sha256`` and ``size_bytes`` are precomputed and stored plaintext
  so the listing path never has to decrypt the body.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from pydantic import BaseModel, field_validator

# URL-safe slug grammar for the storage key. Lowercase / digits /
# dashes / dots / underscores. Allows the historic uppercase form
# (``GCP_CRED``) so legacy rows round-trip through the new validator
# unchanged. Length cap mirrors common URL-segment limits and keeps
# the listing readable at a glance.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_NAME_MAX_LEN = 64

# Hard cap on uploaded file contents. Sized for credential / config
# files: GCP service-account JSON ~3 KB, AWS shared credentials
# <1 KB, kubeconfig with embedded CA bundles ~30 KB, TLS client
# cert+chain (PEM) ~20 KB, pathological CA bundle ~100 KB. 128 KiB
# leaves ~5× headroom for the largest realistic case while making
# an accidental binary / archive upload error immediately. Bump if
# a real use case bumps against it.
MAX_FILE_BYTES: int = 128 * 1024


def compute_sha256(contents: str) -> str:
    """Hex-encoded SHA-256 of ``contents`` for change detection / display."""
    return hashlib.sha256(contents.encode("utf-8")).hexdigest()


class SandboxFile(BaseModel):
    """A per-upstream uploaded file with the plaintext contents attached.

    Constructed in flight only — by the admin tools at write time, by
    the repositories during ``get``. Never serialised back to the UI;
    only :class:`SandboxFileSummary` crosses that boundary.

    ``name`` is the URL-safe storage key. It identifies the file on
    the API path and in the runtime hash; it doesn't export a
    substitution token (``${GCP_CRED}`` resolves against system +
    user Variables only).

    ``display_name`` is the human-readable label shown in the
    dashboard. Defaults to ``name`` when the operator doesn't supply
    one (legacy rows that pre-date this field round-trip with
    ``display_name == name``).

    ``target_path`` is the absolute path inside the sandbox where the
    launcher writes the file. It accepts the same ``${...}``
    substitutions env-var values accept (system + user Variables);
    unknown tokens raise :class:`MissingTemplateVarError` at launch.
    """

    name: str
    display_name: str
    contents: str
    target_path: str
    sha256: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or len(v) > _NAME_MAX_LEN or not _NAME_RE.match(v):
            raise ValueError(
                f"Sandbox file name {v!r} must be 1-{_NAME_MAX_LEN} "
                f"characters, alphanumeric / dash / dot / underscore "
                f"only (matches {_NAME_RE.pattern})"
            )
        return v

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, v: str) -> str:
        # Display name is free-form but must be non-empty (an empty
        # listing row reads as a bug). The repo defaults
        # display_name to ``name`` when the API caller omits it, so
        # an empty value reaches us only on a malformed request.
        if not v.strip():
            raise ValueError("display_name must not be empty")
        return v

    @field_validator("target_path")
    @classmethod
    def _validate_target_path(cls, v: str) -> str:
        if not v:
            raise ValueError("target_path must not be empty")
        # We allow ``${...}/`` *or* an absolute literal path. A
        # relative path would land at an unpredictable location
        # (E2B's ``files.write`` resolves relative paths against
        # the working dir, which the operator doesn't control).
        # Tokens themselves are resolved at launch — system or
        # user Variables are both fair game.
        if not (v.startswith("/") or v.startswith("${")):
            raise ValueError(
                "target_path must be absolute or start with a "
                "${...} reference (e.g. ${HOME}/.config/...)"
            )
        return v


class SandboxFileSummary(BaseModel):
    """View of a sandbox file — sent to the dashboard SPA.

    Never includes ``contents`` — admins upload files but the UI only
    displays size + sha256 metadata. Too easy to leak a credential by
    accident if we render the body verbatim on the detail page.
    """

    name: str
    display_name: str
    target_path: str
    sha256: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime


def is_valid_sandbox_file_name(name: str) -> bool:
    """Public helper so callers don't have to import the private regex."""
    return (
        bool(name)
        and len(name) <= _NAME_MAX_LEN
        and bool(_NAME_RE.match(name))
    )


def slugify_sandbox_file_name(raw: str) -> str:
    """Turn a free-form display name (or uploaded filename) into a
    URL-safe storage key matching :data:`_NAME_RE`.

    Strategy: lowercase; replace runs of disallowed chars with a
    single dash; trim leading / trailing dashes; truncate to
    :data:`_NAME_MAX_LEN`. Returns the empty string when the input
    contains no usable characters — the API caller must then supply
    an explicit ``name`` (or fall back to a UUID-derived id).
    """
    lowered = raw.strip().lower()
    # Collapse any non-allowed run into a single dash, then trim.
    slug = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-")
    if len(slug) > _NAME_MAX_LEN:
        slug = slug[:_NAME_MAX_LEN].rstrip("-")
    return slug
