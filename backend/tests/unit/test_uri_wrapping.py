"""URI wrapping round-trip + decode error semantics."""
from __future__ import annotations

import pytest

from mcpolis.domain.services.uri_wrapping import (
    WrappedUriError,
    is_wrapped_widget_uri,
    unwrap_resource_uri,
    wrap_resource_uri,
    wrap_widget_uri,
)


def test_round_trip_simple_uri() -> None:
    wrapped = wrap_resource_uri(
        org_slug="acme", upstream_id="notion",
        original_uri="test://hello",
    )
    decoded = unwrap_resource_uri(wrapped)
    assert decoded.org_slug == "acme"
    assert decoded.upstream_id == "notion"
    assert decoded.original_uri == "test://hello"
    assert decoded.is_template is False


def test_round_trip_uri_with_path_query_and_fragment() -> None:
    """Slashes / queries / fragments must survive the wrap because the
    base64url encoding sidesteps them."""
    raw = "https://example.invalid/a/b?x=1&y=2#section"
    wrapped = wrap_resource_uri(
        org_slug="acme", upstream_id="notion", original_uri=raw,
    )
    decoded = unwrap_resource_uri(wrapped)
    assert decoded.original_uri == raw


def test_round_trip_uri_with_non_ascii() -> None:
    raw = "https://example.invalid/résumé?q=日本語"
    wrapped = wrap_resource_uri(
        org_slug="acme", upstream_id="notion", original_uri=raw,
    )
    decoded = unwrap_resource_uri(wrapped)
    assert decoded.original_uri == raw


def test_round_trip_template_carries_kind_flag() -> None:
    wrapped = wrap_resource_uri(
        org_slug="acme", upstream_id="notion",
        original_uri="test://things/{id}",
        is_template=True,
    )
    assert "/templates/" in wrapped
    decoded = unwrap_resource_uri(wrapped)
    assert decoded.is_template is True


def test_decode_rejects_unknown_scheme() -> None:
    with pytest.raises(WrappedUriError):
        unwrap_resource_uri("https://example/orgs/a/upstreams/b/resources/x")


def test_decode_rejects_missing_path_segments() -> None:
    with pytest.raises(WrappedUriError):
        unwrap_resource_uri("mcphero://orgs/acme/upstreams/notion")


def test_decode_rejects_unexpected_kind_segment() -> None:
    with pytest.raises(WrappedUriError):
        unwrap_resource_uri(
            "mcphero://orgs/acme/upstreams/notion/widgets/abc"
        )


def test_decode_rejects_bad_base64() -> None:
    with pytest.raises(WrappedUriError):
        unwrap_resource_uri(
            "mcphero://orgs/acme/upstreams/notion/resources/!!!not-b64!!!"
        )


def test_decode_rejects_empty_org_slug_segment() -> None:
    """An empty slug would short-circuit membership checks downstream —
    must be rejected at the parser, not later."""
    wrapped = wrap_resource_uri(
        org_slug="acme", upstream_id="notion", original_uri="test://hello",
    )
    # Surgically replace the slug with empty.
    broken = wrapped.replace("/orgs/acme/", "/orgs//")
    with pytest.raises(WrappedUriError):
        unwrap_resource_uri(broken)


# ─── Widget wrap shape (ui:// scheme) ─────────────────────────────────


def test_wrap_widget_uri_uses_ui_scheme() -> None:
    """MCP Apps clients hard-validate the ``ui://`` prefix on widget
    URIs; a wrap that drops the scheme would make the Inspector
    reject the tool with "Invalid UI resource URI"."""
    wrapped = wrap_widget_uri(
        org_slug="acme", upstream_id="mixpanel",
        original_uri="ui://widget/query-results.html",
    )
    assert wrapped.startswith("ui://mcphero/")
    assert is_wrapped_widget_uri(wrapped)


def test_wrap_widget_uri_round_trips_through_unwrap() -> None:
    raw = "ui://widget/query-results.html"
    wrapped = wrap_widget_uri(
        org_slug="acme", upstream_id="mixpanel", original_uri=raw,
    )
    decoded = unwrap_resource_uri(wrapped)
    assert decoded.org_slug == "acme"
    assert decoded.upstream_id == "mixpanel"
    assert decoded.original_uri == raw
    assert decoded.is_template is False


def test_wrap_widget_uri_includes_widgets_kind_segment() -> None:
    """The ``widgets`` path segment disambiguates from the generic
    ``resources`` / ``templates`` wrap — useful in case both forms
    end up in logs side by side."""
    wrapped = wrap_widget_uri(
        org_slug="acme", upstream_id="mixpanel",
        original_uri="ui://widget/x.html",
    )
    assert "/widgets/" in wrapped


def test_unwrap_resource_uri_rejects_ui_with_wrong_authority() -> None:
    """Only ``ui://mcphero/...`` is one of *our* wrapped widget
    URIs. An upstream's bare ``ui://widget/foo.html`` must NOT be
    treated as wrapped (it has the wrong authority + path layout)."""
    with pytest.raises(WrappedUriError):
        unwrap_resource_uri("ui://widget/query-results.html")


def test_is_wrapped_widget_uri_rejects_unwrapped_ui() -> None:
    """The cheap prefix check must say "no" for upstream-native
    ``ui://`` URIs that haven't been wrapped — otherwise a downstream
    handler might try to unwrap them and crash."""
    assert is_wrapped_widget_uri("ui://widget/query-results.html") is False
    assert is_wrapped_widget_uri("mcphero://orgs/a/upstreams/b/resources/x") is False
