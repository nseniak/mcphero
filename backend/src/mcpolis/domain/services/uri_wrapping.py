"""Wrap upstream resource URIs so the gateway can route a downstream
``resources/read`` back to the right upstream session.

Two parallel wrap shapes:

* **Generic resources / templates** —
    ``mcphero://orgs/{org_slug}/upstreams/{upstream_id}/resources/{b64}``
  (``templates`` instead of ``resources`` for resource templates).

* **MCP Apps widget URIs** —
    ``ui://mcphero/orgs/{org_slug}/upstreams/{upstream_id}/widgets/{b64}``
  Widget URIs MUST keep the ``ui://`` scheme: the MCP Apps spec uses
  it as the marker that says "this is a renderable UI resource",
  and conformant clients (MCP Inspector, Claude Desktop) hard-validate
  the prefix. Without the ``ui://`` scheme they refuse to render the
  resource at all.

Both shapes use the same ``mcphero`` authority + path layout so
the routing logic (org slug + upstream id) stays identical; only the
scheme + leading authority differ. The ``widgets`` path segment
disambiguates from generic resources in case some odd upstream ever
serves a widget URI with the same base64 payload as a regular
resource.

The base64url encoding sidesteps any collision with slashes / queries
/ fragments in the original URI. In single-org mode the wrappers
still bake in ``orgs/{org_slug}`` for shape consistency, with
``org_slug`` set to the standalone org's slug (typically ``default``).

Failures (bad base64, missing segments, unknown upstream) raise
``WrappedUriError`` so the gateway controller can map them to a clear
error response instead of bubbling up a generic decode exception.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass


WRAPPED_SCHEME = "mcphero"
# Widget URIs keep the spec-mandated ``ui://`` scheme. The host
# segment (``mcphero``) is what tells the gateway's
# ``resources/read`` handler this is one of *our* wrapped widget
# URIs and not the upstream's own ``ui://...``.
WIDGET_WRAPPED_SCHEME = "ui"
WIDGET_WRAPPED_AUTHORITY = "mcphero"


class WrappedUriError(ValueError):
    """A wrapped resource URI could not be decoded."""


@dataclass(frozen=True)
class WrappedResource:
    """Decoded form of a wrapped resource URI."""

    org_slug: str
    upstream_id: str
    original_uri: str
    is_template: bool


def _b64_encode(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii").rstrip("=")


def _b64_decode(segment: str) -> str:
    # Restore stripped padding before decoding.
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(
            (segment + padding).encode("ascii"),
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise WrappedUriError(
            f"wrapped URI segment is not valid base64url: {segment!r}",
        ) from exc


def wrap_resource_uri(
    *,
    org_slug: str,
    upstream_id: str,
    original_uri: str,
    is_template: bool = False,
) -> str:
    """Build a wrapped URI that round-trips through ``unwrap_resource_uri``."""
    encoded = _b64_encode(original_uri)
    kind = "templates" if is_template else "resources"
    return (
        f"{WRAPPED_SCHEME}://orgs/{org_slug}/upstreams/"
        f"{upstream_id}/{kind}/{encoded}"
    )


def wrap_widget_uri(
    *,
    org_slug: str,
    upstream_id: str,
    original_uri: str,
) -> str:
    """Wrap a widget URI keeping the spec-required ``ui://`` scheme.

    The MCP Apps client validation regex insists on the ``ui://``
    prefix; rewriting widget URIs to ``mcphero://`` causes the
    Inspector / Claude to reject the tool with "Invalid UI resource
    URI". So widget URIs get a parallel wrap shape that preserves
    the scheme:

        ui://mcphero/orgs/{slug}/upstreams/{id}/widgets/{b64}

    Decoded by ``unwrap_resource_uri`` (which recognises both the
    generic ``mcphero://`` form and this one).
    """
    encoded = _b64_encode(original_uri)
    return (
        f"{WIDGET_WRAPPED_SCHEME}://{WIDGET_WRAPPED_AUTHORITY}/"
        f"orgs/{org_slug}/upstreams/{upstream_id}/widgets/{encoded}"
    )


def is_wrapped_widget_uri(uri: str) -> bool:
    """True if *uri* matches the gateway's ``ui://mcphero/...`` wrap."""
    return uri.startswith(
        f"{WIDGET_WRAPPED_SCHEME}://{WIDGET_WRAPPED_AUTHORITY}/"
    )


def unwrap_resource_uri(uri: str) -> WrappedResource:
    """Decode a wrapped URI back to its parts.

    Recognises both forms:

    * Generic resources / templates: ``mcphero://orgs/.../{kind}/...``
      (``kind`` ∈ ``{resources, templates}``).
    * Widget URIs: ``ui://mcphero/orgs/.../widgets/...``.

    Raises ``WrappedUriError`` on any malformed shape — caller-visible
    failures are normalised so they can be mapped to a single error
    response in the gateway controller. The middleware membership-checks
    the org_slug separately; this helper is a pure parser.
    """
    if "://" not in uri:
        raise WrappedUriError(f"wrapped URI missing scheme: {uri!r}")
    scheme, _, rest = uri.partition("://")
    expected_segments: int
    if scheme == WRAPPED_SCHEME:
        # Generic form: rest = "orgs/{slug}/upstreams/{id}/{kind}/{b64}"
        # 6 segments after the scheme.
        expected_segments = 6
    elif scheme == WIDGET_WRAPPED_SCHEME:
        # Widget form: rest = "mcphero/orgs/{slug}/upstreams/{id}/widgets/{b64}"
        # 7 segments after the scheme (the leading authority is
        # consumed by the ``ui://`` URL parsing model but we keep it
        # in ``rest`` because we split-and-validate rather than parse).
        expected_segments = 7
    else:
        raise WrappedUriError(
            f"wrapped URI has unexpected scheme {scheme!r}",
        )
    parts = rest.split("/")
    if len(parts) != expected_segments:
        raise WrappedUriError(
            f"wrapped URI has wrong number of path segments: {uri!r}",
        )
    if scheme == WIDGET_WRAPPED_SCHEME:
        authority, orgs_label, org_slug, upstreams_label, upstream_id, kind, encoded = parts
        if authority != WIDGET_WRAPPED_AUTHORITY:
            raise WrappedUriError(
                f"widget wrapped URI has unexpected authority: {uri!r}",
            )
        if kind != "widgets":
            raise WrappedUriError(
                f"widget wrapped URI has unexpected kind segment: {kind!r}",
            )
    else:
        orgs_label, org_slug, upstreams_label, upstream_id, kind, encoded = parts
        if kind not in ("resources", "templates"):
            raise WrappedUriError(
                f"wrapped URI has unexpected kind segment: {kind!r}",
            )
    if orgs_label != "orgs" or upstreams_label != "upstreams":
        raise WrappedUriError(
            f"wrapped URI has unexpected path layout: {uri!r}",
        )
    if not org_slug:
        raise WrappedUriError("wrapped URI has empty org slug")
    if not upstream_id:
        raise WrappedUriError("wrapped URI has empty upstream id")
    return WrappedResource(
        org_slug=org_slug,
        upstream_id=upstream_id,
        original_uri=_b64_decode(encoded),
        # Templates only exist in the generic form; widget URIs are
        # always concrete (a widget shell, not a templated URI).
        is_template=(kind == "templates"),
    )
