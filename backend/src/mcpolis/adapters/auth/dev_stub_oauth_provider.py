# pyright: reportUnusedFunction=false
"""Dashboard ``DashboardOAuthProvider`` adapter for local dev / e2e tests.

Issues a real session cookie signed with the same key as production —
the only thing it skips is the Google round-trip. The "login page" is
a tiny in-app HTML form: pick an email, submit. Same cookie issuance,
same role check on subsequent requests as the Google flow.

Refuses to instantiate in cloud mode (defense in depth — startup
validation in ``config.py`` already blocks it, this is a second
guard so a programming mistake can't accidentally wire it up).
"""
from __future__ import annotations

from html import escape
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from mcpolis.domain.ports.dashboard_oauth_provider import CompletedLogin

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Mounted under the dashboard auth router (``/api/auth``).
PICKER_PATH = "/dev-stub/picker"
SUBMIT_PATH = "/dev-stub/submit"


class DevStubOAuthProvider:
    """Stub provider that completes login via an in-app email picker.

    The flow:
    1. ``/api/auth/login`` calls ``start_login`` → returns a redirect
       to the in-app picker page.
    2. The picker form GETs ``/api/auth/dev-stub/submit`` with the
       chosen email, which 302s back to ``/api/auth/callback`` with
       ``code=<email>`` and ``state=<state>``.
    3. ``/api/auth/callback`` calls ``complete_login(code, state, ...)``.
       The provider trusts ``code`` as the email — no token exchange.

    The picker has no own session state: the dashboard's existing
    pending-state dict (one per ``state`` token, 10-min TTL) is the
    single source of truth, just like Google's flow.
    """

    name = "dev_stub"

    def __init__(self, *, mode: str, allow_in_cloud: bool = False) -> None:
        # Defense-in-depth duplicate of ``validate_startup_secrets`` —
        # ``allow_in_cloud=True`` is reserved for the
        # ``cloud + test_mode + loopback`` escape hatch that
        # ``run-e2e-tests.sh`` uses to exercise Mongo/Redis-backed
        # code paths without real Google credentials.
        if mode == "cloud" and not allow_in_cloud:
            raise RuntimeError(
                "DevStubOAuthProvider must not be instantiated in cloud mode",
            )

    async def start_login(
        self,
        *,
        state: str,
        redirect_uri: str,
        join: str | None,
    ) -> str:
        params: dict[str, str] = {
            "state": state,
            "redirect_uri": redirect_uri,
        }
        if join:
            params["join"] = join
        return f"/api/auth{PICKER_PATH}?{urlencode(params)}"

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> CompletedLogin:
        del state, redirect_uri  # state is validated by the route
        if not code or "@" not in code:
            raise ValueError("Dev-stub login requires an email-shaped code")
        return CompletedLogin(email=code)

    def register_routes(
        self,
        router: APIRouter,
        *,
        suggested_emails: list[str] | None = None,
    ) -> None:
        """Mount the picker page + submit handler on the dashboard router.

        ``suggested_emails`` is rendered as a datalist so first-time
        setup is one click. It's a hint only — any email is accepted.
        """
        suggestions = list(suggested_emails or [])

        @router.get(PICKER_PATH, response_class=HTMLResponse)
        async def dev_stub_picker(
            request: Request,
            state: str = Query(...),
            redirect_uri: str = Query(...),
            join: str | None = Query(default=None),
        ) -> HTMLResponse:
            del request
            return HTMLResponse(_render_picker(
                state=state,
                redirect_uri=redirect_uri,
                join=join,
                suggestions=suggestions,
            ))

        @router.get(SUBMIT_PATH)
        async def dev_stub_submit(
            email: str = Query(...),
            state: str = Query(...),
            redirect_uri: str = Query(...),
        ) -> Response:
            email = email.strip()
            if not email or "@" not in email:
                raise HTTPException(400, "Pick a valid email")
            params = urlencode({"code": email, "state": state})
            del redirect_uri  # Always bounce to the dashboard callback.
            return RedirectResponse(
                url=f"/api/auth/callback?{params}", status_code=302,
            )


def _render_picker(
    *,
    state: str,
    redirect_uri: str,
    join: str | None,
    suggestions: list[str],
) -> str:
    """Render the picker HTML. Inline CSS only — keeps the dev page
    independent of the SPA build so it works even on a fresh checkout
    where the frontend hasn't been built."""
    suggestion_options = "".join(
        f'<option value="{escape(s)}">' for s in suggestions
    )
    default_email = suggestions[0] if suggestions else ""
    join_field = (
        f'<input type="hidden" name="join" value="{escape(join)}">'
        if join else ""
    )
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>MCP Hero — dev login</title>
<style>
body {{ font: 16px system-ui, sans-serif; max-width: 32rem; margin: 6rem auto; padding: 0 1rem; }}
h1 {{ font-size: 1.25rem; margin-bottom: 0.25rem; }}
p.note {{ color: #666; font-size: 0.9rem; margin-top: 0; }}
form {{ display: flex; gap: 0.5rem; margin-top: 2rem; }}
input[type=text] {{ flex: 1; padding: 0.5rem 0.75rem; border: 1px solid #ccc; border-radius: 0.375rem; font: inherit; }}
button {{ padding: 0.5rem 1rem; background: #2563eb; color: white; border: 0; border-radius: 0.375rem; font: inherit; cursor: pointer; }}
button:hover {{ background: #1d4ed8; }}
.suggestions {{ margin-top: 1rem; color: #666; font-size: 0.85rem; }}
.suggestions code {{ background: #f1f1f1; padding: 0.1rem 0.3rem; border-radius: 0.2rem; }}
</style>
</head><body>
<h1>MCP Hero — dev login</h1>
<p class="note">No Google round-trip. The cookie you receive is real and signed with the production code path.</p>
<form method="get" action="/api/auth{SUBMIT_PATH}">
  <input type="hidden" name="state" value="{escape(state)}">
  <input type="hidden" name="redirect_uri" value="{escape(redirect_uri)}">
  {join_field}
  <input type="text" name="email" list="dev-stub-emails" value="{escape(default_email)}" placeholder="you@example.test" autofocus required>
  <button type="submit">Sign in</button>
</form>
<datalist id="dev-stub-emails">{suggestion_options}</datalist>
{_render_suggestion_block(suggestions)}
</body></html>"""
    return body


def _render_suggestion_block(suggestions: list[str]) -> str:
    if not suggestions:
        return ""
    items = " ".join(
        f'<code>{escape(s)}</code>' for s in suggestions
    )
    return f'<p class="suggestions">Suggested: {items}</p>'
