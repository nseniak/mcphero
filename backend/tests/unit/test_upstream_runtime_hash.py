"""Tests for the upstream runtime-hash helper.

Pure function, no I/O. Verifies the hash is stable across argument
orderings, sensitive to every input that affects runtime behaviour,
and insensitive to fields that don't (timestamps on the upstream
itself, the env var's plaintext, the env-var ``last_four`` preview —
all of which are derived from inputs the hash already covers via
``updated_at``).
"""
from __future__ import annotations

from datetime import UTC, datetime

from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.template_var import TemplateVarSummary
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.services.upstream_runtime_hash import (
    compute_upstream_runtime_hash,
)


def _make_stdio_upstream(
    *,
    command: str = "npx",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cpu_vcpus: float = 1.0,
    memory_mb: int = 1024,
    auth_mode: AuthMode = AuthMode.service_account,
) -> UpstreamDefinition:
    return UpstreamDefinition(
        id="github",
        display_name="GitHub",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command=command,
            args=args or [],
            env=env or {},
            cpu_vcpus=cpu_vcpus,
            memory_mb=memory_mb,
        ),
        auth=UpstreamAuthConfig(mode=auth_mode),
    )


def _make_http_upstream(
    *,
    url: str = "https://example.test/mcp",
    headers: dict[str, str] | None = None,
    auth_mode: AuthMode = AuthMode.service_account,
) -> UpstreamDefinition:
    return UpstreamDefinition(
        id="example",
        display_name="Example",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=url, headers=headers or {}),
        auth=UpstreamAuthConfig(mode=auth_mode),
    )


def _make_summary(
    *,
    name: str,
    is_secret: bool = True,
    updated_at: datetime | None = None,
) -> TemplateVarSummary:
    when = updated_at or datetime(2026, 5, 4, 10, 0, tzinfo=UTC)
    return TemplateVarSummary(
        name=name,
        is_secret=is_secret,
        value=None if is_secret else "debug",
        last_four="cdef" if is_secret else None,
        created_at=when,
        updated_at=when,
    )


def test_hash_is_deterministic_for_same_inputs() -> None:
    upstream = _make_stdio_upstream(env={"A": "${X}"})
    summaries = [_make_summary(name="X")]
    assert (
        compute_upstream_runtime_hash(upstream, summaries)
        == compute_upstream_runtime_hash(upstream, summaries)
    )


def test_hash_insensitive_to_env_var_argument_order() -> None:
    upstream = _make_stdio_upstream()
    a = _make_summary(name="ALPHA")
    b = _make_summary(name="BETA")
    assert (
        compute_upstream_runtime_hash(upstream, [a, b])
        == compute_upstream_runtime_hash(upstream, [b, a])
    )


def test_hash_insensitive_to_stdio_env_dict_order() -> None:
    a = _make_stdio_upstream(env={"A": "1", "B": "2"})
    b = _make_stdio_upstream(env={"B": "2", "A": "1"})
    assert compute_upstream_runtime_hash(a, []) == compute_upstream_runtime_hash(b, [])


def test_hash_changes_when_env_var_updated_at_changes() -> None:
    upstream = _make_stdio_upstream()
    older = _make_summary(
        name="X",
        updated_at=datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
    )
    newer = _make_summary(
        name="X",
        updated_at=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
    )
    assert (
        compute_upstream_runtime_hash(upstream, [older])
        != compute_upstream_runtime_hash(upstream, [newer])
    )


def test_hash_changes_when_stdio_command_changes() -> None:
    a = _make_stdio_upstream(command="npx")
    b = _make_stdio_upstream(command="bunx")
    assert compute_upstream_runtime_hash(a, []) != compute_upstream_runtime_hash(b, [])


def test_hash_changes_when_sandbox_resources_change() -> None:
    a = _make_stdio_upstream(memory_mb=1024)
    b = _make_stdio_upstream(memory_mb=2048)
    assert compute_upstream_runtime_hash(a, []) != compute_upstream_runtime_hash(b, [])


def test_hash_changes_when_http_url_changes() -> None:
    a = _make_http_upstream(url="https://a.test/mcp")
    b = _make_http_upstream(url="https://b.test/mcp")
    assert compute_upstream_runtime_hash(a, []) != compute_upstream_runtime_hash(b, [])


def test_hash_changes_when_http_headers_change() -> None:
    a = _make_http_upstream(headers={"X-Source": "foo"})
    b = _make_http_upstream(headers={"X-Source": "bar"})
    assert compute_upstream_runtime_hash(a, []) != compute_upstream_runtime_hash(b, [])


def test_hash_does_not_carry_secret_plaintext() -> None:
    """The hash input must never include ``TemplateVarSummary.value`` for
    secret rows — they are masked everywhere, and a hash that depends
    on plaintext would force decryption on every read."""
    upstream = _make_stdio_upstream()
    when = datetime(2026, 5, 4, 10, 0, tzinfo=UTC)
    a = TemplateVarSummary(
        name="SECRET", is_secret=True, value=None, last_four="aaaa",
        created_at=when, updated_at=when,
    )
    b = TemplateVarSummary(
        name="SECRET", is_secret=True, value=None, last_four="bbbb",
        created_at=when, updated_at=when,
    )
    # Different ``last_four`` (would only happen if plaintext changed)
    # but identical updated_at — value/last_four are not part of the
    # hash payload, ``updated_at`` is the proxy.
    assert (
        compute_upstream_runtime_hash(upstream, [a])
        == compute_upstream_runtime_hash(upstream, [b])
    )
