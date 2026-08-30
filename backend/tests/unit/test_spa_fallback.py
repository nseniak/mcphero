"""Tests for ``_should_reject_spa_fallback``.

Context: the SPA catch-all in ``create_app`` used to serve
``index.html`` with status 200 for *any* unmatched path. Production
logs showed vulnerability scanners probing ``/api/checkorder.php``,
``/.well-known/pki-validation/*.php``, ``phpmyadmin``-adjacent paths,
etc., all receiving 200s. That makes the deployment look like a live
PHP target and masks honest 404s from the backend API surface.

Pinning three properties:

1. Backend-route prefixes (``api/``, ``mcp/``, ``admin-mcp/``,
   ``.well-known/``) never fall back to the SPA shell. A real
   backend route wins by registration order, so a request reaching
   the catch-all under one of these means no handler matched — a 404
   is the correct answer, not the React app.
2. Server-side-script extensions are rejected regardless of prefix,
   so mixed probes like ``/weird/path/login.php`` don't slip through.
3. Legitimate SPA routes (``/``, ``/dashboard``, ``/org/foo/bar``,
   etc.) are *not* rejected — the fix must not regress the actual
   client-side-routed app.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.entrypoints.app import (  # pyright: ignore[reportPrivateUsage]
    _resolve_under,
    _should_reject_spa_fallback,
)


# ── Legitimate SPA routes: must pass through ─────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "",                                    # "/" — root
        "dashboard",
        "admin",
        "admin/users",
        "my-tools",
        "org/acme/upstreams",
        "org/acme/upstreams/notion",
        "connect/notion",
        "some/deeply/nested/route",
        # Hyphens, digits, mixed — all valid React Router paths.
        "org/org-with-dashes/page-42",
    ],
)
def test_legit_spa_route_not_rejected(path: str) -> None:
    assert _should_reject_spa_fallback(path) is False


# ── Backend prefixes: must 404 honestly ──────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "api/something-that-doesnt-exist",
        "api/admin/upstreams/ghost",
        "api/graphql",                # POST probes came through; GET would land here
        "mcp/something",
        "mcp/acme/nonexistent",
        "admin-mcp/ghost",
        ".well-known/security.txt",
        ".well-known/pki-validation/doc.php",
        ".well-known/acme-challenge/xeV8HbJYmNm",  # scanner ACME probe
    ],
)
def test_backend_prefix_rejected(path: str) -> None:
    assert _should_reject_spa_fallback(path) is True


# ── Server-side-script extensions: must 404 ──────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "checkorder.php",
        "index.php",
        "login.asp",
        "auth.aspx",
        "struts2.jsp",
        "cgi-bin/thing.cgi",
        # Mixed case — scanners do this.
        "Admin.PHP",
        "INDEX.Php",
        # Deep paths still rejected by extension.
        "weird/deep/path/config.env",
        "some/other/path/dump.sql",
    ],
)
def test_scanner_extension_rejected(path: str) -> None:
    assert _should_reject_spa_fallback(path) is True


# ── Edge cases that have caused real regressions in similar code ─────


def test_reject_is_case_insensitive_on_prefix() -> None:
    """Scanners capitalize prefixes to try to slip past naive checks."""
    assert _should_reject_spa_fallback("API/ghost") is True
    assert _should_reject_spa_fallback("Api/anything") is True
    assert _should_reject_spa_fallback(".Well-Known/foo") is True


def test_non_matching_extension_not_rejected() -> None:
    """Not every extension is scanner-bait. ``.html`` files, for
    example, might legitimately be requested (even if Vite builds
    don't produce them at odd paths) — we only reject the set we
    explicitly listed."""
    assert _should_reject_spa_fallback("report.html") is False
    assert _should_reject_spa_fallback("data.json") is False
    assert _should_reject_spa_fallback("config.yaml") is False


def test_substring_match_does_not_count_as_prefix() -> None:
    """``api`` only matches at the start — a route like
    ``rapidapi/something`` is a valid SPA route, not an API call."""
    assert _should_reject_spa_fallback("rapidapi/something") is False
    assert _should_reject_spa_fallback("my-api-docs") is False


def test_empty_path_is_not_rejected() -> None:
    """The root (``"/"``) arrives here as the empty string. That's
    the SPA entry point — the most load-bearing route of all."""
    assert _should_reject_spa_fallback("") is False


# --- path-escape guard (_resolve_under) ---
#
# The catch-all joins the URL path onto the built frontend directory.
# Starlette strips exactly one leading slash when it binds
# ``{path:path}``, so ``//proc/self/environ`` arrives as the absolute
# ``/proc/self/environ`` — and ``dist / "/proc/self/environ"`` is just
# ``/proc/self/environ`` under pathlib rules. Meerbot (sister project,
# same code) was probed for exactly that on 2026-08-30. mcphero.io is
# shielded by nginx, which merges duplicate slashes and forwards only
# fixed prefixes; the ``standalone`` compose profile has no such proxy.


def make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>shell</html>")
    (dist / "favicon.svg").write_text("<svg></svg>")
    (dist / "contact").mkdir()
    (dist / "contact" / "index.html").write_text("<html>contact</html>")
    return dist


def test_resolve_under_allows_real_file(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    assert _resolve_under(dist, "favicon.svg") == dist / "favicon.svg"


def test_resolve_under_allows_nested_prerendered_route(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    resolved = _resolve_under(dist, "contact", "index.html")
    assert resolved == dist / "contact" / "index.html"


def test_resolve_under_allows_the_root_itself(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    assert _resolve_under(dist, "") == dist


def test_resolve_under_rejects_absolute_path(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("MCPOLIS_SESSION_SECRET=hunter2")
    assert _resolve_under(dist, str(secret)) is None


def test_resolve_under_rejects_proc_self_environ(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    assert _resolve_under(dist, "/proc/self/environ") is None


def test_resolve_under_rejects_dotdot_escape(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    (tmp_path / "outside.txt").write_text("not public")
    assert _resolve_under(dist, "../outside.txt") is None


def test_resolve_under_rejects_dotdot_buried_mid_path(tmp_path: Path) -> None:
    dist = make_dist(tmp_path)
    (tmp_path / "outside.txt").write_text("not public")
    assert _resolve_under(dist, "contact/../../outside.txt") is None


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "/proc/self/cmdline", "//etc/hostname", "../../../etc/passwd"],
)
def test_resolve_under_rejects_known_scanner_probes(tmp_path: Path, path: str) -> None:
    dist = make_dist(tmp_path)
    assert _resolve_under(dist, path) is None
