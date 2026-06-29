"""ARQ jobs that deliver sales notification emails (Stage 3 Slice 5a)."""

import logging
import uuid

from app.db.session import SessionLocal
from app.db.models.lead import Lead
from app.db.models.meeting import Meeting
from app.db.models.quote import Quote
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifStatus
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

_REP_SUBJECT = {
    NotifEvent.SALES_READY_LEAD: "New sales-ready lead",
    NotifEvent.MEETING_BOOKED: "New demo booked",
    NotifEvent.QUOTE_GENERATED: "New quote generated",
}


def _entity_context(db, log: NotificationLog) -> dict:
    """Build the template context for the rep alert from the log's entity."""
    if log.entity_type == "lead":
        lead = db.get(Lead, log.entity_id)
        return {
            "kind": "lead",
            "name": lead.name if lead else None,
            "company": lead.company if lead else None,
            "score": lead.lead_score if lead else None,
        }
    if log.entity_type == "meeting":
        m = db.get(Meeting, log.entity_id)
        lead = db.get(Lead, m.lead_id) if m else None
        return {
            "kind": "meeting",
            "scheduled_at": m.scheduled_at if m else None,
            "name": lead.name if lead else None,
            "company": lead.company if lead else None,
        }
    # default: quote
    q = db.get(Quote, log.entity_id)
    lead = db.get(Lead, q.lead_id) if q else None
    return {
        "kind": "quote",
        "total": q.total if q else None,
        "currency": q.currency if q else None,
        "name": lead.name if lead else None,
        "company": lead.company if lead else None,
    }


async def send_rep_alert(ctx, notification_log_id: str) -> None:
    """Send a rep-alert email for a sales notification log entry.

    Idempotent: exits early if the log is missing or already marked sent.
    Marks the log `sent` on success, `failed` on error, then re-raises so
    ARQ can retry.
    """
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id))
        if log is None or log.status == NotifStatus.SENT:
            return
        ctx_data = _entity_context(db, log)
        ok = EmailService().send_templated_email(
            to_email=log.recipient,
            subject=_REP_SUBJECT.get(log.event, "Sales notification"),
            template_name="sales_rep_alert.html",
            context=ctx_data,
        )
        log.status = NotifStatus.SENT if ok else NotifStatus.FAILED
        if not ok:
            log.error = "send_templated_email returned False"
        db.commit()
    except Exception as exc:  # noqa: BLE001 — mark failed, let ARQ retry
        logger.error(f"send_rep_alert failed: {exc}")
        try:
            log = db.get(NotificationLog, uuid.UUID(notification_log_id))
            if log is not None:
                log.status = NotifStatus.FAILED
                log.error = str(exc)[:500]
                db.commit()
        except Exception:
            pass
        raise
    finally:
        db.close()


async def send_visitor_meeting_confirmation(ctx, notification_log_id: str) -> None:
    """Send a visitor-facing meeting confirmation email.

    Idempotent: exits early if the log is missing or already marked sent.
    Marks the log `sent` on success, `failed` on error, then re-raises so
    ARQ can retry.
    """
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id))
        if log is None or log.status == NotifStatus.SENT:
            return
        m = db.get(Meeting, log.entity_id)
        ok = EmailService().send_templated_email(
            to_email=log.recipient,
            subject="Your demo is booked",
            template_name="sales_meeting_confirmation.html",
            context={
                "scheduled_at": m.scheduled_at if m else None,
                "visitor_timezone": m.visitor_timezone if m else None,
            },
        )
        log.status = NotifStatus.SENT if ok else NotifStatus.FAILED
        if not ok:
            log.error = "send_templated_email returned False"
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"send_visitor_meeting_confirmation failed: {exc}")
        try:
            log = db.get(NotificationLog, uuid.UUID(notification_log_id))
            if log is not None:
                log.status = NotifStatus.FAILED
                log.error = str(exc)[:500]
                db.commit()
        except Exception:
            pass
        raise
    finally:
        db.close()
