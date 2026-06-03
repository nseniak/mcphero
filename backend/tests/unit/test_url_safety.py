"""Unit tests for ``mcpolis.domain.services.url_safety``.

The validator is the first line of defence against SSRF on
user-supplied upstream MCP URLs (security finding F-01). Each test
stubs ``socket.getaddrinfo`` so DNS never leaves the process.

Toplevel test functions, no classes, no fixtures — per CLAUDE.md.
"""
from __future__ import annotations

import socket
from collections.abc import Callable

import pytest

from mcpolis.domain.services.url_safety import (
    UnsafeUpstreamUrl,
    validate_upstream_url,
)


def _stub_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]],
) -> None:
    monkeypatch.delenv(
        "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK", raising=False,
    )
    """Stub ``socket.getaddrinfo`` so the validator's resolver returns
    a deterministic list of A/AAAA records for the given host.

    ``mapping`` is ``{host: [ip, ...]}``. Family is inferred from the
    address shape — colons → AF_INET6, otherwise AF_INET. Numeric
    literals (``socket.AI_NUMERICHOST``) bypass the stub by returning
    a single record matching the literal so ``validate_upstream_url``'s
    "is the host already an IP literal?" path works without the stub
    needing to handle it specially.
    """
    real = socket.getaddrinfo

    def fake(
        host: str | None, port: object, *args: object, **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        flags = 0
        if len(args) >= 4:
            flags = int(args[3])  # type: ignore[arg-type]
        elif "flags" in kwargs:
            flags = int(kwargs["flags"])  # type: ignore[arg-type]
        if flags & socket.AI_NUMERICHOST:
            return real(host, port, *args, **kwargs)  # type: ignore[arg-type]
        if host is None or host not in mapping:
            raise socket.gaierror(f"unstubbed host: {host!r}")
        records: list[tuple[int, int, int, str, tuple[object, ...]]] = []
        for ip in mapping[host]:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr: tuple[object, ...]
            if family == socket.AF_INET6:
                sockaddr = (ip, 0, 0, 0)
            else:
                sockaddr = (ip, 0)
            records.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return records

    monkeypatch.setattr(socket, "getaddrinfo", fake)


# ----------------------------------------------------------------- reject


def test_rejects_imds_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url(
            "http://169.254.169.254/latest/meta-data/iam/",
        )


def test_rejects_loopback_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://127.0.0.1:8080/")


def test_rejects_rfc1918_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://10.0.0.5/")


def test_rejects_rfc1918_172_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://172.16.5.10/")


def test_rejects_rfc1918_192_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://192.168.1.1/")


def test_rejects_zero_dotted(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://0.0.0.0/")


def test_rejects_cgnat(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://100.64.0.1/")


def test_rejects_multicast(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://239.0.0.1/")


def test_rejects_public_name_resolving_to_imds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_getaddrinfo(
        monkeypatch, {"internal.example.com": ["169.254.169.254"]},
    )
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://internal.example.com/")


def test_rejects_when_any_record_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-record A: even one private record poisons the lookup."""
    _stub_getaddrinfo(
        monkeypatch,
        {"multi.example.com": ["8.8.8.8", "169.254.169.254"]},
    )
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://multi.example.com/")


def test_rejects_ipv6_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://[::1]/")


def test_rejects_ipv6_ula(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://[fc00::1]/")


def test_rejects_ipv6_link_local(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://[fe80::1]/")


def test_rejects_ipv4_mapped_ipv6_to_imds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://[::ffff:169.254.169.254]/")


def test_rejects_unresolvable_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_getaddrinfo(monkeypatch, {})  # nothing maps → gaierror
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http://nope.invalid/")


def test_rejects_non_http_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("ftp://example.com/")


def test_rejects_missing_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    with pytest.raises(UnsafeUpstreamUrl):
        validate_upstream_url("http:///path-only")


# ----------------------------------------------------------------- accept


def test_accepts_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    resolved = validate_upstream_url("http://1.1.1.1/")
    assert resolved.url == "http://1.1.1.1/"
    assert resolved.host == "1.1.1.1"
    assert "1.1.1.1" in resolved.resolved_ips


def test_accepts_public_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(
        monkeypatch, {"upstream.example.com": ["8.8.8.8", "1.1.1.1"]},
    )
    resolved = validate_upstream_url(
        "https://upstream.example.com:8443/mcp",
    )
    assert resolved.host == "upstream.example.com"
    assert set(resolved.resolved_ips) == {"8.8.8.8", "1.1.1.1"}


def test_accepts_public_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_getaddrinfo(monkeypatch, {})
    resolved = validate_upstream_url("https://[2606:4700:4700::1111]/")
    assert "2606:4700:4700::1111" in resolved.resolved_ips


def test_resolver_called_each_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``validate_upstream_url`` re-resolves on every call (no cache).

    This is what defeats the create-time→connect-time DNS rebind
    window: the transport calls the validator again at connect time
    and gets a fresh lookup.
    """
    calls: list[str] = []
    real = socket.getaddrinfo

    def counting(
        host: str | None, port: object, *args: object, **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        if host is not None:
            calls.append(host)
        flags = 0
        if len(args) >= 4:
            flags = int(args[3])  # type: ignore[arg-type]
        elif "flags" in kwargs:
            flags = int(kwargs["flags"])  # type: ignore[arg-type]
        if flags & socket.AI_NUMERICHOST or host == "1.2.3.4":
            return [(
                socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0),
            )]
        return real(host, port, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.delenv(
        "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK", raising=False,
    )
    monkeypatch.setattr(socket, "getaddrinfo", counting)
    validate_upstream_url("http://1.2.3.4/")
    validate_upstream_url("http://1.2.3.4/")
    assert len([c for c in calls if c == "1.2.3.4"]) >= 2


# Force-import the Callable symbol so unused-import linting accepts
# its presence in the typing surface above. The stub helper takes
# ``Callable``-compatible callables but we don't bind one explicitly.
_ = Callable
