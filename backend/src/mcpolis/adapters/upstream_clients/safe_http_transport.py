"""SSRF-safe ``httpx`` transport for outbound MCP / OAuth requests.

Sits in front of ``httpx.AsyncHTTPTransport`` and re-validates every
outbound URL through ``validate_upstream_url`` before forwarding to
the underlying transport. This catches:

- Literal IP URLs in deny-listed ranges (loopback, RFC1918, IMDS,
  IPv6 ULA / link-local, IPv4-mapped IMDS, CGNAT, etc.).
- Hostname URLs whose A/AAAA records currently resolve into a denied
  range (DNS rebind between create-time and connect-time).
- Cross-host redirects to a denied target — ``follow_redirects=True``
  causes ``httpx`` to call ``handle_async_request`` again on the new
  URL, which trips the same validator.

A test-only escape hatch (``MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK=1``)
permits 127.0.0.0/8 so unit tests can spin up a local HTTP server and
exercise the transport's redirect behaviour. Production must NEVER
set this flag — startup-config validation should treat its presence
as a misconfiguration.

Residual gap: between our resolver call and ``httpcore``'s own
internal resolver call, an attacker who fully controls DNS for a
target hostname could in principle race the cache. Closing that
window requires re-implementing the connection pool to pin sockets
to a pre-validated IP, which interacts badly with TLS SNI / cert
hostname validation; the validator at create-time + per-request
re-validation closes every realistic SSRF path in our threat model
without breaking HTTPS upstreams.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

import httpx

from mcpolis.domain.services.url_safety import (
    UnsafeUpstreamUrl,
    validate_upstream_url,
)

_TEST_LOOPBACK_FLAG = "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK"


def _is_loopback_url(url: str) -> bool:
    """True iff the URL's host is 127.0.0.0/8 or ``::1``.

    Used to gate the test-only loopback escape hatch — never relax
    the deny-list for any other range.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    return addr.is_loopback


class SafeAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """``httpx`` async transport that validates every outbound URL.

    Drop-in replacement for ``httpx.AsyncHTTPTransport``: pass the
    same kwargs through (``verify``, ``cert``, ``trust_env``, etc.),
    and inject this transport into ``httpx.AsyncClient(transport=...)``
    or any caller that accepts a custom transport.
    """

    async def handle_async_request(
        self, request: httpx.Request,
    ) -> httpx.Response:
        url_str = str(request.url)
        try:
            validate_upstream_url(url_str)
        except UnsafeUpstreamUrl:
            if (
                os.environ.get(_TEST_LOOPBACK_FLAG) == "1"
                and _is_loopback_url(url_str)
            ):
                # Test escape hatch: only loopback, only when the env
                # flag is on. Any other denied range still raises.
                return await super().handle_async_request(request)
            raise
        return await super().handle_async_request(request)
