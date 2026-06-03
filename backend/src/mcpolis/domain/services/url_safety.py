"""Validate user-supplied upstream MCP URLs against an SSRF deny-list.

This module is the create-time / connect-time gate that closes
security finding F-01: an org admin must not be able to register an
HTTP MCP whose URL targets the EC2 instance metadata service or any
private/loopback range, then exfiltrate the host's temporary AWS
credentials via the connection-error surface.

The validator is intentionally pure and stateless. The runtime
backstop (``SafeAsyncHTTPTransport``) re-calls it on every outbound
request so a DNS rebind between create-time and connect-time is also
caught.

Deny-list (covers IPv4 and IPv6):

- IPv4: ``127.0.0.0/8`` (loopback), ``10.0.0.0/8`` / ``172.16.0.0/12``
  / ``192.168.0.0/16`` (RFC1918), ``169.254.0.0/16`` (link-local —
  catches IMDS), ``0.0.0.0/8``, ``100.64.0.0/10`` (CGNAT),
  ``224.0.0.0/4`` (multicast), ``240.0.0.0/4`` (reserved).
- IPv6: ``::1`` (loopback), ``fc00::/7`` (ULA), ``fe80::/10``
  (link-local), ``ff00::/8`` (multicast), ``::/128`` (unspecified).
- IPv4-mapped IPv6 (``::ffff:0:0/96``): the embedded IPv4 address is
  re-checked through the IPv4 list, so ``[::ffff:169.254.169.254]``
  is refused as IMDS rather than passing as "public IPv6".
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

_TEST_LOOPBACK_FLAG = "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK"


class UnsafeUpstreamUrl(Exception):
    """Raised when an upstream URL targets a deny-listed range.

    Carrying both the offending URL and a short reason lets the
    create-time handler render a structured 400 and the runtime
    backstop log a single line per refusal.
    """

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"unsafe upstream URL {url!r}: {reason}")
        self.url = url
        self.reason = reason


@dataclass(frozen=True)
class ResolvedUrl:
    """Outcome of a successful ``validate_upstream_url`` call.

    ``resolved_ips`` is the (de-duplicated) set of A/AAAA records the
    resolver returned at validation time. The transport may pin the
    socket to one of these IPs to defeat a TOCTOU rebind between
    validation and connect.
    """

    url: str
    scheme: str
    host: str
    port: int
    resolved_ips: tuple[str, ...]


def _is_safe_ip(ip: str) -> tuple[bool, str | None]:
    """Return ``(safe, reason_if_unsafe)`` for a single literal IP.

    ``ipaddress`` knows about every well-known private / reserved /
    multicast range. We layer an explicit IPv4-mapped-IPv6 unwrap on
    top so ``::ffff:169.254.169.254`` is judged as IMDS, not as a
    benign IPv6 address.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, f"not a valid IP literal: {ip!r}"

    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_safe_ip(str(addr.ipv4_mapped))

    if addr.is_loopback:
        # Test/dev escape hatch: ``MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK=1``
        # permits 127.0.0.0/8 and ``::1`` so the dev demo MCP (mounted
        # at ``http://localhost:8080/dev/mcp-demo``) and e2e fixtures
        # (test MCP servers on 127.0.0.1) can still register. The flag
        # MUST NOT be set in prod — startup-config validation in
        # ``entrypoints/config.py`` enforces this.
        if os.environ.get(_TEST_LOOPBACK_FLAG) == "1":
            return True, None
        return False, f"loopback address {addr}"
    if addr.is_link_local:
        return False, f"link-local address {addr}"
    if addr.is_private:
        return False, f"private address {addr}"
    if addr.is_multicast:
        return False, f"multicast address {addr}"
    if addr.is_unspecified:
        return False, f"unspecified address {addr}"
    if addr.is_reserved:
        return False, f"reserved address {addr}"

    if isinstance(addr, ipaddress.IPv4Address):
        # CGNAT 100.64.0.0/10 — `ipaddress` flags this as `is_global`
        # = False (since Python 3.13 returns True; safer to check).
        if addr in ipaddress.IPv4Network("100.64.0.0/10"):
            return False, f"CGNAT address {addr}"
    return True, None


def _resolve_host(host: str) -> tuple[str, ...]:
    """Resolve ``host`` to its A and AAAA records.

    The numeric path (literal IP in the URL) is handled by the
    ``AI_NUMERICHOST`` short-circuit so a literal in the URL doesn't
    accidentally trigger a real DNS lookup.
    """
    try:
        try:
            infos = socket.getaddrinfo(
                host, None,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=0,
                flags=socket.AI_NUMERICHOST,
            )
        except socket.gaierror:
            infos = socket.getaddrinfo(
                host, None,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=0,
            )
    except socket.gaierror as exc:
        raise UnsafeUpstreamUrl(
            host, f"DNS resolution failed: {exc}",
        ) from exc

    resolved: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0] if sockaddr else ""
        if isinstance(ip, str) and ip and ip not in resolved:
            resolved.append(ip)
    if not resolved:
        raise UnsafeUpstreamUrl(host, "DNS returned no addresses")
    return tuple(resolved)


def validate_upstream_url(url: str) -> ResolvedUrl:
    """Validate ``url`` against the SSRF deny-list.

    Raises ``UnsafeUpstreamUrl`` on every kind of failure (bad scheme,
    missing host, unresolvable name, any A/AAAA record in a denied
    range). Returns a ``ResolvedUrl`` carrying the host and the
    resolved IP list when every check passes.
    """
    if not url:
        raise UnsafeUpstreamUrl(str(url), "URL must be a non-empty string")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeUpstreamUrl(
            url, f"only http/https schemes are allowed, got {scheme!r}",
        )

    host = parts.hostname
    if not host:
        raise UnsafeUpstreamUrl(url, "URL is missing a host")

    port = parts.port or (443 if scheme == "https" else 80)

    resolved = _resolve_host(host)
    for ip in resolved:
        ok, reason = _is_safe_ip(ip)
        if not ok:
            raise UnsafeUpstreamUrl(
                url,
                f"host {host!r} resolves to denied {reason}",
            )

    return ResolvedUrl(
        url=url, scheme=scheme, host=host, port=port,
        resolved_ips=resolved,
    )
