"""The analytics value naming an OAuth MCP's server never carries the
user or password an MCP URL may hold before its host."""
from __future__ import annotations

from mcpolis.entrypoints.routes.dashboard.auth_connect import oauth_provider_domain


def test_the_provider_domain_drops_the_user_and_password_of_the_url() -> None:
    domain = oauth_provider_domain(
        "https://alice:s3cr3t-pass@mcp.example.com:8443/mcp",
    )

    assert domain == "mcp.example.com"


def test_the_provider_domain_of_a_plain_url_is_its_host() -> None:
    assert oauth_provider_domain("https://mcp.linear.app/sse") == "mcp.linear.app"


def test_the_provider_domain_of_a_url_with_no_host_is_empty() -> None:
    assert oauth_provider_domain("not a url") == ""
