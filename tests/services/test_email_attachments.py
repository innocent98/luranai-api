"""Tests for EmailService binary-attachment support."""

from unittest.mock import patch

from app.services.email_service import EmailService


def test_send_email_with_attachment_builds_mixed_message():
    captured = {}

    def fake_send_message(msg):
        captured["msg"] = msg

    class FakeServer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            fake_send_message(msg)

    with patch("app.services.email_service.smtplib.SMTP_SSL", return_value=FakeServer()):
        ok = EmailService()._send_email(
            to_email="rep@co.com",
            subject="Quote",
            html_content="<p>hi</p>",
            attachments=[("quote-1.pdf", b"%PDF-1.4 fake", "application/pdf")],
        )

    assert ok is True
    msg = captured["msg"]
    assert msg.get_content_subtype() == "mixed"
    filenames = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert "quote-1.pdf" in filenames


def test_send_email_without_attachment_is_alternative():
    class FakeServer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            pass

    with patch("app.services.email_service.smtplib.SMTP_SSL", return_value=FakeServer()):
        ok = EmailService()._send_email(
            to_email="r@co.com",
            subject="s",
            html_content="<p>x</p>",
        )

    assert ok is True
