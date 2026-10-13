"""Tests for ARQ sales-notification email jobs (Stage 3 Slice 5a)."""

import pytest
from uuid import uuid4
from unittest.mock import patch

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.workers.sales_notifications import send_rep_alert, send_visitor_meeting_confirmation


class _NoCloseSession:
    """Thin proxy around a SQLAlchemy Session that swallows close() calls.

    The worker always calls db.close() in its finally block. Without this
    proxy the test session would be closed mid-test, making the post-call
    assertion fail with a detached-instance error.
    """

    def __init__(self, session):
        self._session = session

    def close(self):
        pass  # do not close the shared test session

    def __getattr__(self, name):
        return getattr(self._session, name)


def _seed_quote_log(db):
    """Insert a Tenant → Lead → Quote → NotificationLog chain; return log.id."""
    t = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="T",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()

    lead = Lead(
        tenant_id=t.id,
        conversation_id=uuid4(),
        email="v@co.com",
        company="Acme",
    )
    db.add(lead)
    db.flush()

    q = Quote(
        tenant_id=t.id,
        lead_id=lead.id,
        currency="NGN",
        line_items=[{"label": "Growth", "detail": "", "amount": 45000}],
        total=45000,
        status=QuoteStatus.DRAFT,
    )
    db.add(q)
    db.flush()

    log = NotificationLog(
        tenant_id=t.id,
        event=NotifEvent.QUOTE_GENERATED,
        channel=NotifChannel.EMAIL,
        entity_type="quote",
        entity_id=q.id,
        recipient="rep@co.com",
        status=NotifStatus.PENDING,
    )
    db.add(log)
    db.commit()
    return log.id


async def test_send_rep_alert_marks_sent(db_session, monkeypatch):
    """send_rep_alert flips status to SENT and calls send_templated_email once.

    render_quote_pdf is forced to fail so the fallback plain-email path is used;
    the SENT outcome is what this test asserts.
    """
    log_id = _seed_quote_log(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch(
        "app.workers.sales_notifications.render_quote_pdf",
        side_effect=RuntimeError("pdf unavailable in test"),
    ):
        with patch(
            "app.services.email_service.EmailService.send_templated_email", return_value=True
        ) as send:
            await send_rep_alert({}, str(log_id))

    send.assert_called_once()
    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.SENT


async def test_send_rep_alert_idempotent_if_already_sent(db_session, monkeypatch):
    """send_rep_alert exits early and does not call EmailService when already SENT."""
    log_id = _seed_quote_log(db_session)
    # Pre-mark as sent
    log = db_session.get(NotificationLog, log_id)
    log.status = NotifStatus.SENT
    db_session.commit()

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch(
        "app.services.email_service.EmailService.send_templated_email", return_value=True
    ) as send:
        await send_rep_alert({}, str(log_id))

    send.assert_not_called()


async def test_send_rep_alert_marks_failed_on_email_error(db_session, monkeypatch):
    """send_rep_alert marks FAILED and raises RuntimeError when EmailService returns False.

    render_quote_pdf is forced to fail so the plain-email fallback is reached;
    that call then returns False, producing the FAILED outcome this test asserts.
    """
    log_id = _seed_quote_log(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch(
        "app.workers.sales_notifications.render_quote_pdf",
        side_effect=RuntimeError("pdf unavailable in test"),
    ):
        with patch(
            "app.services.email_service.EmailService.send_templated_email", return_value=False
        ):
            with pytest.raises(RuntimeError, match="email send failed"):
                await send_rep_alert({}, str(log_id))

    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.FAILED


def _seed_meeting_log(db):
    """Insert a Tenant → Lead → Meeting → NotificationLog chain; return (log.id, meeting.id)."""
    from app.db.models.meeting import Meeting, MeetingStatus
    from datetime import datetime, timezone

    t = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="T",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()

    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="visitor@co.com", company="Acme")
    db.add(lead)
    db.flush()

    meeting = Meeting(
        tenant_id=t.id,
        lead_id=lead.id,
        scheduled_at=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        visitor_timezone="Africa/Lagos",
        status=MeetingStatus.SCHEDULED,
    )
    db.add(meeting)
    db.flush()

    log = NotificationLog(
        tenant_id=t.id,
        event=NotifEvent.MEETING_BOOKED,
        channel=NotifChannel.EMAIL,
        entity_type="meeting",
        entity_id=meeting.id,
        recipient="visitor@co.com",
        status=NotifStatus.PENDING,
    )
    db.add(log)
    db.commit()
    return log.id


async def test_send_visitor_meeting_confirmation_marks_sent(db_session, monkeypatch):
    """send_visitor_meeting_confirmation flips status to SENT and calls send_templated_email."""
    log_id = _seed_meeting_log(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch(
        "app.services.email_service.EmailService.send_templated_email", return_value=True
    ) as send:
        await send_visitor_meeting_confirmation({}, str(log_id))

    send.assert_called_once()
    refreshed = db_session.get(NotificationLog, log_id)
    assert refreshed.status == NotifStatus.SENT


async def test_send_visitor_confirmation_idempotent_if_already_sent(db_session, monkeypatch):
    """send_visitor_meeting_confirmation exits early without calling EmailService when already SENT."""
    log_id = _seed_meeting_log(db_session)
    log = db_session.get(NotificationLog, log_id)
    log.status = NotifStatus.SENT
    db_session.commit()

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch(
        "app.services.email_service.EmailService.send_templated_email", return_value=True
    ) as send:
        await send_visitor_meeting_confirmation({}, str(log_id))

    send.assert_not_called()
    refreshed = db_session.get(NotificationLog, log_id)
    assert refreshed.status == NotifStatus.SENT


async def test_send_visitor_confirmation_marks_failed_on_email_error(db_session, monkeypatch):
    """send_visitor_meeting_confirmation marks FAILED and raises RuntimeError when EmailService returns False."""
    log_id = _seed_meeting_log(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("app.services.email_service.EmailService.send_templated_email", return_value=False):
        with pytest.raises(RuntimeError, match="email send failed"):
            await send_visitor_meeting_confirmation({}, str(log_id))

    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.FAILED
