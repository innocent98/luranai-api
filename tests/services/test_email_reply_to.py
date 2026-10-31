"""EmailService: optional Reply-To and a sender display name, with header-injection guards."""

from unittest.mock import patch

from app.core.config import settings
from app.services.email_service import EmailService


class _FakeServer:
    sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def starttls(self, *a, **k):
        pass

    def send_message(self, msg):
        _FakeServer.sent.append(msg)


def _send(**kwargs):
    _FakeServer.sent = []
    with (
        patch("app.services.email_service.smtplib.SMTP_SSL", return_value=_FakeServer()),
        patch("app.services.email_service.smtplib.SMTP", return_value=_FakeServer()),
    ):
        ok = EmailService()._send_email(
            to_email="v@example.com", subject="s", html_content="<p>x</p>", **kwargs
        )
    assert ok is True
    return _FakeServer.sent[0]


def test_no_reply_to_header_unless_asked():
    msg = _send()
    assert msg["Reply-To"] is None


def test_reply_to_is_set_on_plain_and_attachment_messages():
    assert _send(reply_to="rep@example.com")["Reply-To"] == "rep@example.com"
    with_pdf = _send(reply_to="rep@example.com", attachments=[("q.pdf", b"%PDF", "application/pdf")])
    assert with_pdf["Reply-To"] == "rep@example.com"


def test_from_name_replaces_the_display_name_but_not_the_address(monkeypatch):
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@luran.example")
    msg = _send(from_name="Acme Ltd")
    assert msg["From"] == "Acme Ltd <noreply@luran.example>"


def test_header_values_cannot_inject_extra_headers(monkeypatch):
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "noreply@luran.example")
    msg = _send(from_name="Acme\r\nBcc: evil@example.com", reply_to="rep@example.com\r\nBcc: evil@example.com")
    assert msg["Bcc"] is None
    assert "\n" not in msg["From"] and "\r" not in msg["From"]
    assert "\n" not in msg["Reply-To"] and "\r" not in msg["Reply-To"]
