"""Stable fingerprint of the inputs that decide an upstream's runtime
behaviour, used by the dashboard's "stop & restart" dirty banner.

When an upstream is started, the manager snapshots this hash on its
``UpstreamState``. On every detail-page read we recompute it from the
*current* persisted config + env-var set; if the running snapshot's
hash differs, the upstream is "dirty" — the user has saved changes
that won't take effect until they stop and start the MCP.

The hash inputs are everything that flows into the connection task at
start time:

- transport + stdio / http config (command, args, env, url, headers,
  sandbox resources)
- auth mode and scopes (credentials don't ride the hash; rotation
  alone shouldn't trigger a banner — but ``client_id`` does, since
  swapping OAuth apps mid-run breaks the running session)
- ``default_arguments`` (injected into every tool call)
- per-MCP env-var summaries — fingerprinted by ``(name, is_secret,
  updated_at)``. Secret plaintexts never enter the hash; the
  ``updated_at`` timestamp is the proxy because every replace /
  delete bumps it.

The hash must be reproducible on any process that reads the same
inputs — so canonical JSON (sorted keys, no whitespace) feeds
SHA-256.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from mcpolis.domain.model.sandbox_file import SandboxFileSummary
from mcpolis.domain.model.template_var import TemplateVarSummary
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    UpstreamDefinition,
)


def _stdio_payload(stdio: StdioTransportConfig | None) -> dict[str, Any] | None:
    if stdio is None:
        return None
    # Sort env keys so insertion-order doesn't change the hash.
    return {
        "command": stdio.command,
        "args": list(stdio.args),
        "env": dict(sorted(stdio.env.items())),
        "cpu_vcpus": stdio.cpu_vcpus,
        "memory_mb": stdio.memory_mb,
        "disk_gb": stdio.disk_gb,
        "pids_limit": stdio.pids_limit,
        "tmpfs_mb": stdio.tmpfs_mb,
        "persistent_disk_enabled": stdio.persistent_disk_enabled,
    }


def _http_payload(http: HttpTransportConfig | None) -> dict[str, Any] | None:
    if http is None:
        return None
    return {
        "url": http.url,
        "headers": dict(sorted(http.headers.items())),
    }


def _template_vars_payload(
    summaries: list[TemplateVarSummary],
) -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "is_secret": s.is_secret,
            "updated_at": s.updated_at.isoformat(),
        }
        for s in sorted(summaries, key=lambda s: s.name)
    ]


def _sandbox_files_payload(
    summaries: list[SandboxFileSummary],
) -> list[dict[str, Any]]:
    """Fingerprint of per-MCP Sandbox files for dirty-banner detection.

    Includes ``name`` (substitution token), ``target_path`` (the
    sandbox-side path the file lands at — changes its semantics for
    the MCP), ``sha256`` (content fingerprint — changes mean the
    file body changed), and ``updated_at`` (proxy for any
    metadata-only changes that don't bump sha256).

    File contents are never read into the hash; the precomputed
    sha256 stands in for the body so the dirty path stays
    encryption-cheap.
    """
    return [
        {
            "name": s.name,
            "target_path": s.target_path,
            "sha256": s.sha256,
            "updated_at": s.updated_at.isoformat(),
        }
        for s in sorted(summaries, key=lambda s: s.name)
    ]


def compute_upstream_runtime_hash(
    upstream: UpstreamDefinition,
    template_var_summaries: list[TemplateVarSummary],
    sandbox_file_summaries: list[SandboxFileSummary] | None = None,
) -> str:
    """Return a stable SHA-256 hex digest of the runtime-relevant inputs.

    ``sandbox_file_summaries`` defaults to ``None`` for backwards
    compatibility with the legacy test factories that pre-date the
    Sandbox-files feature; passing ``None`` is treated identically
    to passing ``[]`` so a fresh upstream with no files matches the
    historical hash. Once the manager is wired with a
    :class:`SandboxFileRepository` (cloud + standalone), every real
    call site supplies the live list so file edits drive the dirty
    banner uniformly with Variable edits.
    """
    payload: dict[str, Any] = {
        "transport": upstream.transport.value,
        "auth_mode": upstream.auth.mode.value,
        "auth_scopes": sorted(upstream.auth.scopes),
        "auth_client_id": upstream.auth.client_id,
        "stdio": _stdio_payload(upstream.stdio),
        "http": _http_payload(upstream.http),
        "default_arguments": upstream.default_arguments,
        "template_vars": _template_vars_payload(template_var_summaries),
        "sandbox_files": _sandbox_files_payload(sandbox_file_summaries or []),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
