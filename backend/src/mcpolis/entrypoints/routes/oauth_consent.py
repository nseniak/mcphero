"""Gateway OAuth consent route — the confused-deputy gate's HTTP surface.

After Google authenticates the user, the gateway parks any not-yet-
approved client here instead of forwarding the authorization code. This
route renders a page naming the client and the exact host the code would
be sent to, and carries the user's explicit approve/deny decision back
into the provider. No code is forwarded without a human POST from this
page.

The consent token in the query/form is an unguessable, single-use secret
minted by the provider for this one parked flow; it doubles as the CSRF
token for the POST.
"""
from __future__ import annotations

import html

import structlog
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    ConsentPrompt,
    McpGatewayOAuthProvider,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# User-facing brand (the codename ``mcpolis`` never reaches the user).
_BRAND = "MCP Hero"

# The consent page is a security decision surface. Forbid framing so it
# can't be clickjacked into a hidden Approve, and send no referrer so the
# consent token in the URL never leaks to another origin.
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _render_page(prompt: ConsentPrompt, consent_token: str) -> str:
    """Build the consent page. Every value derived from the client (name,
    redirect) is HTML-escaped — a hostile client controls them."""
    client_name = html.escape(prompt.client_name or prompt.client_id)
    redirect_host = html.escape(prompt.redirect_host)
    redirect_uri = html.escape(prompt.redirect_uri)
    token_attr = html.escape(consent_token)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize access · {_BRAND}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 34rem; margin: 3rem auto;
         padding: 0 1rem; color: #1a1a1a; }}
  .card {{ border: 1px solid #e0e0e0; border-radius: 12px; padding: 1.5rem 1.75rem; }}
  .dest {{ background: #f6f6f6; border-radius: 8px; padding: .5rem .75rem;
           font-family: ui-monospace, monospace; word-break: break-all; }}
  .warn {{ color: #8a5300; font-size: .9rem; }}
  .row {{ display: flex; gap: .75rem; margin-top: 1.5rem; }}
  button {{ flex: 1; padding: .7rem 1rem; border-radius: 8px; font-size: 1rem;
            cursor: pointer; border: 1px solid #ccc; }}
  .approve {{ background: #1a7f37; color: #fff; border-color: #1a7f37; }}
  .deny {{ background: #fff; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Authorize access</h1>
    <p><strong>{client_name}</strong> is asking to connect to your
       {_BRAND} account.</p>
    <p>If you approve, {_BRAND} will send your sign-in result to this
       address:</p>
    <p class="dest" title="{redirect_uri}">{redirect_host}</p>
    <p class="warn">Approve only if you started this connection and you
       recognize the address above. If you did not, choose Deny.</p>
    <form method="post" action="/mcp/oauth/consent" class="row">
      <input type="hidden" name="consent" value="{token_attr}">
      <button type="submit" name="decision" value="deny" class="deny">Deny</button>
      <button type="submit" name="decision" value="approve" class="approve">Approve</button>
    </form>
  </div>
</body>
</html>"""


async def _handle_consent(request: Request) -> Response:
    provider: McpGatewayOAuthProvider = (
        request.app.state.mcp_gateway_oauth_provider
    )

    if request.method == "GET":
        consent_token = request.query_params.get("consent", "")
        prompt = await provider.render_consent(consent_token)
        if prompt is None:
            return HTMLResponse(
                "<h1>Request expired</h1><p>This authorization request is "
                "no longer valid. Start the connection again.</p>",
                status_code=404,
                headers=_SECURITY_HEADERS,
            )
        return HTMLResponse(
            _render_page(prompt, consent_token), headers=_SECURITY_HEADERS
        )

    # POST — carry the human decision back into the provider.
    form = await request.form()
    consent_token = str(form.get("consent", ""))
    decision = str(form.get("decision", ""))
    if decision not in ("approve", "deny"):
        return HTMLResponse(
            "<h1>Invalid request</h1><p>Missing approve/deny decision.</p>",
            status_code=400,
            headers=_SECURITY_HEADERS,
        )

    try:
        redirect_url = await provider.resolve_consent(
            consent_token, approve=decision == "approve"
        )
    except ValueError as exc:
        logger.warning("oauth.consent.resolve_failed", error=str(exc))
        return HTMLResponse(
            "<h1>Request expired</h1><p>This authorization request is no "
            "longer valid. Start the connection again.</p>",
            status_code=404,
            headers=_SECURITY_HEADERS,
        )

    return RedirectResponse(
        url=redirect_url, status_code=302, headers=_SECURITY_HEADERS
    )


def create_consent_route() -> Route:
    return Route(
        "/oauth/consent",
        endpoint=_handle_consent,
        methods=["GET", "POST"],
    )
