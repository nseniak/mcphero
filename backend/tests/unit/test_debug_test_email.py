"""Tests for the super-admin ``POST /api/debug/test-email`` endpoint.

Builds a minimal FastAPI app with just the debug router so the test
exercises the route — superadmin gate, recipient validation, and the
transport-reporting contract — without standing up the whole app. The
``EmailSender`` is injected (a ``StubEmailSender`` to assert on what
would have been sent, or a deliberately failing fake) rather than
patched.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpolis.adapters.email.stub_email_sender import StubEmailSender
from mcpolis.adapters.observability.analytics_client import AnalyticsClient
from mcpolis.domain.ports.email_sender import EmailSender
from mcpolis.entrypoints.routes.debug_routes import create_debug_router


SUPERADMIN = "boss@mcphero.io"


def make_client(
    *,
    email_sender: EmailSender,
    current_user: str = SUPERADMIN,
) -> TestClient:
    analytics = AnalyticsClient(token="", super_properties={})

    def get_current_user() -> str:
        return current_user

    app = FastAPI()
    app.include_router(
        create_debug_router(
            analytics,
            email_sender,
            get_current_user,
            {SUPERADMIN},
        )
    )
    return TestClient(app, raise_server_exceptions=False)


class FailingEmailSender:
    async def send_email(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None:
        raise RuntimeError("smtp auth rejected")


def test_test_email_sends_via_stub_and_reports_transport() -> None:
    stub = StubEmailSender()
    client = make_client(email_sender=stub)

    resp = client.post("/api/debug/test-email", json={"to": "me@example.com"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is True
    assert body["to"] == "me@example.com"
    assert body["transport"] == "StubEmailSender"
    # Stub is a no-op transport — must not be reported as real delivery.
    assert body["real_delivery"] is False
    assert len(stub.sent) == 1
    assert stub.sent[0].to == "me@example.com"
    assert "MCP Hero" in stub.sent[0].subject


def test_test_email_trims_recipient() -> None:
    stub = StubEmailSender()
    client = make_client(email_sender=stub)

    resp = client.post("/api/debug/test-email", json={"to": "  me@example.com  "})

    assert resp.status_code == 200
    assert stub.sent[0].to == "me@example.com"


def test_test_email_rejects_invalid_recipient() -> None:
    stub = StubEmailSender()
    client = make_client(email_sender=stub)

    resp = client.post("/api/debug/test-email", json={"to": "not-an-email"})

    assert resp.status_code == 400
    assert stub.sent == []


def test_test_email_non_superadmin_gets_404() -> None:
    stub = StubEmailSender()
    client = make_client(email_sender=stub, current_user="rando@example.com")

    resp = client.post("/api/debug/test-email", json={"to": "me@example.com"})

    # 404 (not 403) so the debug routes look nonexistent to non-admins.
    assert resp.status_code == 404
    assert stub.sent == []


def test_test_email_send_failure_returns_502() -> None:
    client = make_client(email_sender=FailingEmailSender())

    resp = client.post("/api/debug/test-email", json={"to": "me@example.com"})

    assert resp.status_code == 502
    assert "smtp auth rejected" in resp.json()["detail"]
