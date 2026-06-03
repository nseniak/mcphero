"""Resolve instance-level OAuth app credentials by matching upstream URL domain."""
from __future__ import annotations

from urllib.parse import urlparse

from mcpolis.domain.model.settings import OAuthAppEntry, OAuthAppsConfig


def resolve_oauth_app(
    url: str, config: OAuthAppsConfig
) -> tuple[OAuthAppEntry, str] | None:
    """Return (entry, matched_domain) if hostname ends with a configured domain."""
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return None
    for domain, entry in config.items():
        if hostname == domain or hostname.endswith("." + domain):
            return entry, domain
    return None
