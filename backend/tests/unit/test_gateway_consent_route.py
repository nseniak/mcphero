"""SEC-CONFUSED-DEPUTY, part 3 — the consent route renders and resolves.

Part 2 proves the provider *decides* to stop at consent and remembers an
approval. This proves the HTTP surface: ``GET /oauth/consent`` renders a
human page naming the client and redirect host (and does NOT silently
auto-submit or auto-redirect, which would defeat the gate), and
``POST /oauth/consent`` carries the human's approve/deny decision back
into the provider and 302s to wherever the provider says.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient
from starlette.applications import Starlette

from mcpolis.entrypoints.routes.oauth_consent import create_consent_route


@dataclass
class _Prompt:
    client_id: str
    client_name: str | None
    redirect_uri: str
    redirect_host: str


class _StubProvider:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, bool]] = []

    async def render_consent(self, token: str) -> _Prompt | None:
        if token == "good":
            return _Prompt(
                client_id="c1",
                client_name="Totally Legit MCP",
                redirect_uri="https://attacker.example/grab",
                redirect_host="attacker.example",
            )
        return None

    async def resolve_consent(self, token: str, approve: bool) -> str:
        self.resolved.append((token, approve))
        if approve:
            return "https://attacker.example/grab?code=xyz&state=s"
        return "https://attacker.example/grab?error=access_denied&state=s"


def make_app(provider: object) -> Starlette:
    app = Starlette(routes=[create_consent_route()])
    app.state.mcp_gateway_oauth_provider = provider
    return app


def test_get_consent_renders_client_and_host_without_auto_submit() -> None:
    client = TestClient(make_app(_StubProvider()), raise_server_exceptions=False)
    resp = client.get("/oauth/consent?consent=good")
    assert resp.status_code == 200
    body = resp.text
    # Names the client and the exact host the code would be sent to.
    assert "attacker.example" in body
    assert "Totally Legit MCP" in body
    # Offers an explicit human choice.
    assert "approve" in body.lower()
    assert "deny" in body.lower()
    # Must NOT auto-advance the flow for the victim.
    assert "http-equiv" not in body.lower()
    assert "form.submit()" not in body.replace(" ", "")


def test_get_consent_sets_anti_framing_headers() -> None:
    """Review finding 5. The consent page is a decision surface — it must
    forbid framing (anti-clickjacking) and send no referrer."""
    client = TestClient(make_app(_StubProvider()), raise_server_exceptions=False)
    resp = client.get("/oauth/consent?consent=good")
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_get_consent_unknown_token_is_404() -> None:
    client = TestClient(make_app(_StubProvider()), raise_server_exceptions=False)
    resp = client.get("/oauth/consent?consent=nope")
    assert resp.status_code == 404


def test_post_consent_approve_redirects_with_code() -> None:
    provider = _StubProvider()
    client = TestClient(
        make_app(provider), raise_server_exceptions=False, follow_redirects=False
    )
    resp = client.post(
        "/oauth/consent", data={"consent": "good", "decision": "approve"}
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://attacker.example/grab?code=xyz&state=s"
    assert provider.resolved == [("good", True)]


def test_post_consent_deny_redirects_without_code() -> None:
    provider = _StubProvider()
    client = TestClient(
        make_app(provider), raise_server_exceptions=False, follow_redirects=False
    )
    resp = client.post(
        "/oauth/consent", data={"consent": "good", "decision": "deny"}
    )
    assert resp.status_code == 302
    assert "code=" not in resp.headers["location"]
    assert provider.resolved == [("good", False)]


def test_post_consent_missing_decision_is_400() -> None:
    provider = _StubProvider()
    client = TestClient(make_app(provider), raise_server_exceptions=False)
    resp = client.post("/oauth/consent", data={"consent": "good"})
    assert resp.status_code == 400
    assert provider.resolved == []
