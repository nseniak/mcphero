"""Tests for the real SMTP ``EmailSender`` adapter.

``SmtpEmailSender`` is the production transport for the §5.2 re-auth
notifier (Google Workspace submission in prod). The unit under test
is everything *except* the network: MIME message construction (From
alias vs auth user, plain vs HTML), the port→TLS-mode inference, and
the raise-on-failure contract the §5.2 sweep relies on for retries.

The ``aiosmtplib.send`` call is injected as ``send_fn`` so the tests
record the message + connection kwargs without opening a socket —
dependency injection in place of patching.
"""
from __future__ import annotations

from email.message import EmailMessage

import pytest

from mcpolis.adapters.email.smtp_email_sender import SmtpEmailSender


def make_recorder() -> tuple[list[tuple[EmailMessage, dict[str, object]]], object]:
    """Return (calls, send_fn). ``send_fn`` records each invocation."""
    calls: list[tuple[EmailMessage, dict[str, object]]] = []

    async def send_fn(message: EmailMessage, **kwargs: object) -> object:
        calls.append((message, kwargs))
        return ({}, "OK")

    return calls, send_fn


def make_sender(
    send_fn: object,
    *,
    port: int = 587,
    from_name: str = "MCP Hero",
) -> SmtpEmailSender:
    return SmtpEmailSender(
        host="smtp.gmail.com",
        port=port,
        username="robot@mcphero.io",
        password="app-password",
        from_addr="info@mcphero.io",
        from_name=from_name,
        send_fn=send_fn,  # type: ignore[arg-type]
    )


async def test_plain_text_message_headers_and_body() -> None:
    calls, send_fn = make_recorder()
    sender = make_sender(send_fn)

    await sender.send_email(
        to="admin@example.com",
        subject="Reconnect your integration",
        body_text="Click the link to re-auth.",
    )

    assert len(calls) == 1
    message, _kwargs = calls[0]
    # From stamps the alias + brand display name, not the auth user.
    assert message["From"] == "MCP Hero <info@mcphero.io>"
    assert message["To"] == "admin@example.com"
    assert message["Subject"] == "Reconnect your integration"
    assert not message.is_multipart()
    assert message.get_content_type() == "text/plain"
    assert "Click the link to re-auth." in message.get_content()


async def test_connection_kwargs_use_auth_user_and_password() -> None:
    calls, send_fn = make_recorder()
    sender = make_sender(send_fn)

    await sender.send_email(to="a@b.com", subject="s", body_text="b")

    _message, kwargs = calls[0]
    assert kwargs["hostname"] == "smtp.gmail.com"
    assert kwargs["port"] == 587
    assert kwargs["username"] == "robot@mcphero.io"
    assert kwargs["password"] == "app-password"


async def test_html_alternative_added() -> None:
    calls, send_fn = make_recorder()
    sender = make_sender(send_fn)

    await sender.send_email(
        to="admin@example.com",
        subject="s",
        body_text="plain fallback",
        body_html="<p>rich</p>",
    )

    message, _kwargs = calls[0]
    assert message.is_multipart()
    assert message.get_content_type() == "multipart/alternative"
    subtypes = [part.get_content_type() for part in message.iter_parts()]
    assert subtypes == ["text/plain", "text/html"]


async def test_starttls_for_port_587() -> None:
    calls, send_fn = make_recorder()
    sender = make_sender(send_fn, port=587)

    await sender.send_email(to="a@b.com", subject="s", body_text="b")

    _message, kwargs = calls[0]
    assert kwargs["start_tls"] is True
    assert kwargs["use_tls"] is False


async def test_implicit_tls_for_port_465() -> None:
    calls, send_fn = make_recorder()
    sender = make_sender(send_fn, port=465)

    await sender.send_email(to="a@b.com", subject="s", body_text="b")

    _message, kwargs = calls[0]
    assert kwargs["use_tls"] is True
    assert kwargs["start_tls"] is False


async def test_from_without_display_name() -> None:
    calls, send_fn = make_recorder()
    sender = make_sender(send_fn, from_name="")

    await sender.send_email(to="a@b.com", subject="s", body_text="b")

    message, _kwargs = calls[0]
    assert message["From"] == "info@mcphero.io"


async def test_send_failure_propagates() -> None:
    # The §5.2 sweep only marks-notified after a successful send, so the
    # adapter must let a transport error bubble up rather than swallow it.
    async def failing_send_fn(message: EmailMessage, **kwargs: object) -> object:
        raise RuntimeError("smtp refused")

    sender = make_sender(failing_send_fn)

    with pytest.raises(RuntimeError, match="smtp refused"):
        await sender.send_email(to="a@b.com", subject="s", body_text="b")
