"""Tests for PDF attachment in send_rep_alert (Stage 3 Slice 5b)."""

from uuid import uuid4
from unittest.mock import patch

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.workers.sales_notifications import send_rep_alert


class _NoCloseSession:
    """Proxy around a SQLAlchemy Session that swallows close() calls.

    The worker always calls db.close() in its finally block; without this
    proxy the shared test session would be closed mid-test, raising
    detached-instance errors on the post-call assertions.
    """

    def __init__(self, session):
        self._session = session

    def close(self):
        pass  # do not close the shared test session

    def __getattr__(self, name):
        return getattr(self._session, name)


def _seed_quote_log(db):
    """Insert Tenant → Lead → Quote → NotificationLog (QUOTE_GENERATED); return (log.id, quote.id)."""
    t = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="T",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
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
    return log.id, q.id


def _seed_lead_log(db):
    """Insert Tenant → Lead → NotificationLog (SALES_READY_LEAD); return log.id."""
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
        email="lead@co.com",
        company="LeadCo",
    )
    db.add(lead)
    db.flush()
    log = NotificationLog(
        tenant_id=t.id,
        event=NotifEvent.SALES_READY_LEAD,
        channel=NotifChannel.EMAIL,
        entity_type="lead",
        entity_id=lead.id,
        recipient="rep@co.com",
        status=NotifStatus.PENDING,
    )
    db.add(log)
    db.commit()
    return log.id


async def test_quote_rep_alert_has_pdf_attachment(db_session, monkeypatch):
    """quote event + successful render → send_templated_email_with_attachments called with quote-<id>.pdf."""
    log_id, quote_id = _seed_quote_log(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch(
        "app.workers.sales_notifications.render_quote_pdf", return_value=b"%PDF-test"
    ):
        with patch(
            "app.services.email_service.EmailService.send_templated_email_with_attachments",
            return_value=True,
        ) as send:
            await send_rep_alert({}, str(log_id))

    send.assert_called_once()
    call_kwargs = send.call_args.kwargs
    # attachments may come in as kwarg or positional arg 4
    attachments = call_kwargs.get("attachments") or (
        send.call_args.args[4] if len(send.call_args.args) > 4 else None
    )
    assert attachments is not None, "attachments kwarg must be present"
    assert len(attachments) == 1
    filename, content, mime = attachments[0]
    assert filename == f"quote-{quote_id}.pdf"
    assert content == b"%PDF-test"
    assert mime == "application/pdf"

    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.SENT


async def test_quote_rep_alert_sends_text_when_pdf_fails(db_session, monkeypatch):
    """render_quote_pdf raises → plain email still sent; log resolves SENT (graceful degradation)."""
    log_id, _ = _seed_quote_log(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch(
        "app.workers.sales_notifications.render_quote_pdf",
        side_effect=RuntimeError("boom"),
    ):
        with patch(
            "app.services.email_service.EmailService.send_templated_email", return_value=True
        ) as send_plain:
            await send_rep_alert({}, str(log_id))

    send_plain.assert_called_once()
    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.SENT


async def test_lead_rep_alert_has_no_attachment(db_session, monkeypatch):
    """Non-quote events (SALES_READY_LEAD) use plain send_templated_email — no PDF attachment."""
    log_id = _seed_lead_log(db_session)

    import app.workers.sales_notifications as notif_mod

    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch(
        "app.services.email_service.EmailService.send_templated_email", return_value=True
    ) as send_plain:
        with patch(
            "app.services.email_service.EmailService.send_templated_email_with_attachments"
        ) as send_attach:
            await send_rep_alert({}, str(log_id))

    send_plain.assert_called_once()
    send_attach.assert_not_called()
    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.SENT
