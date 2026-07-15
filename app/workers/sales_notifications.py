"""ARQ jobs that deliver sales notification emails, Slack messages, and CRM webhooks (Stage 3 Slice 5a/5d/5e)."""

import logging
import uuid

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.db.models.lead import Lead
from app.db.models.meeting import Meeting
from app.db.models.quote import Quote
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.services.email_service import EmailService
from app.services.quote_pdf_service import render_quote_pdf
from app.utils.network_security import ensure_safe_outbound_url

logger = logging.getLogger(__name__)


class _EmailSendFailed(RuntimeError):
    """Sentinel raised when send_templated_email returns False.

    Subclasses RuntimeError so existing pytest.raises(RuntimeError) assertions
    still pass.  The outer except-handler checks for this type and re-raises
    immediately, skipping the fallback mark-FAILED path (which already ran).
    """


_REP_SUBJECT = {
    NotifEvent.SALES_READY_LEAD: "New sales-ready lead",
    NotifEvent.MEETING_BOOKED: "New meeting booked",
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
        if log.event == NotifEvent.QUOTE_GENERATED:
            quote = db.get(Quote, log.entity_id)
            lead = db.get(Lead, quote.lead_id) if quote else None
            pdf_bytes = None
            try:
                if quote is not None:
                    pdf_bytes = render_quote_pdf(quote, lead=lead)
            except (
                Exception
            ) as pdf_exc:  # noqa: BLE001 — PDF is an enhancement, never block the alert
                logger.warning(f"quote PDF render failed (sending text alert): {pdf_exc}")
            if pdf_bytes:
                ok = EmailService().send_templated_email_with_attachments(
                    to_email=log.recipient,
                    subject=_REP_SUBJECT.get(log.event, "Sales notification"),
                    template_name="sales_rep_alert.html",
                    context=ctx_data,
                    attachments=[(f"quote-{log.entity_id}.pdf", pdf_bytes, "application/pdf")],
                )
            else:
                ok = EmailService().send_templated_email(
                    to_email=log.recipient,
                    subject=_REP_SUBJECT.get(log.event, "Sales notification"),
                    template_name="sales_rep_alert.html",
                    context=ctx_data,
                )
        else:
            ok = EmailService().send_templated_email(
                to_email=log.recipient,
                subject=_REP_SUBJECT.get(log.event, "Sales notification"),
                template_name="sales_rep_alert.html",
                context=ctx_data,
            )
        if not ok:
            log.status = NotifStatus.FAILED
            log.error = "send_templated_email returned False"
            db.commit()
            raise _EmailSendFailed("email send failed")
        log.status = NotifStatus.SENT
        db.commit()
    except Exception as exc:  # noqa: BLE001 — mark failed, let ARQ retry
        if isinstance(exc, _EmailSendFailed):
            raise  # already persisted FAILED with precise error; skip fallback
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
            subject="Your meeting is booked",
            template_name="sales_meeting_confirmation.html",
            context={
                "scheduled_at": m.scheduled_at if m else None,
                "visitor_timezone": m.visitor_timezone if m else None,
            },
        )
        if not ok:
            log.status = NotifStatus.FAILED
            log.error = "send_templated_email returned False"
            db.commit()
            raise _EmailSendFailed("email send failed")
        log.status = NotifStatus.SENT
        db.commit()
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, _EmailSendFailed):
            raise  # already persisted FAILED with precise error; skip fallback
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


# ---------------------------------------------------------------------------
# Slack delivery (Stage 3 Slice 5d)
# ---------------------------------------------------------------------------


def _slack_blocks(title: str | None, body: str | None, dashboard_url: str = "") -> dict:
    """Build a minimal Block Kit payload with a header, section, and dashboard link block."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": (title or "Sales notification")[:150],
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (body or "")[:2000] or "—",
            },
        },
    ]
    if dashboard_url:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"<{dashboard_url}|View in dashboard>",
                    }
                ],
            }
        )
    return {"blocks": blocks}


async def send_slack_notification(ctx, notification_log_id: str) -> None:
    """POST a Block Kit message to the tenant's Slack webhook.

    Idempotent: exits early if the log is missing or already SENT.
    Status-based retry strategy:
      - 2xx  → SENT, return.
      - 5xx or network/timeout → FAILED + raise _EmailSendFailed (ARQ retries).
      - 4xx  → FAILED, return (permanent client error, no retry).
      - no webhook URL configured → FAILED, return (no raise).

    The webhook URL is NEVER written to the log.
    """
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id))
        if log is None or log.status == NotifStatus.SENT:
            return

        cfg = db.execute(
            select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == log.tenant_id)
        ).scalar_one_or_none()
        url = cfg.slack_webhook_url if cfg else None

        if not url:
            log.status = NotifStatus.FAILED
            log.error = "no slack webhook configured"
            db.commit()
            return

        dashboard_url = f"{settings.FRONTEND_URL}/sales/leads"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(url, json=_slack_blocks(log.title, log.body, dashboard_url))
        except Exception as exc:  # network / timeout — transient, retry
            log.status = NotifStatus.FAILED
            log.error = f"slack network error: {type(exc).__name__}"
            db.commit()
            raise _EmailSendFailed("slack network error") from exc

        if 200 <= resp.status_code < 300:
            log.status = NotifStatus.SENT
            db.commit()
            return

        if resp.status_code >= 500:
            log.status = NotifStatus.FAILED
            log.error = f"slack {resp.status_code}"
            db.commit()
            raise _EmailSendFailed(f"slack {resp.status_code}")  # 5xx → ARQ retry

        # 4xx — permanent client error, mark failed, do not retry
        log.status = NotifStatus.FAILED
        log.error = f"slack {resp.status_code}"
        db.commit()

    except _EmailSendFailed:
        raise  # already persisted FAILED; skip fallback
    except Exception as exc:  # noqa: BLE001
        logger.error(f"send_slack_notification failed: {exc}")
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CRM webhook delivery (Stage 3 Slice 5e)
# ---------------------------------------------------------------------------

_CRM_ID_KEYS = ("id", "crm_id", "contactId", "vid")


def _crm_payload(lead: Lead) -> dict:
    """Build the outbound lead payload. Contains decrypted PII — never log this."""
    return {
        "lead_id": str(lead.id),
        "tenant_id": str(lead.tenant_id) if lead.tenant_id else None,
        "name": lead.name,
        "email": lead.email,
        "company": lead.company,
        "team_size": lead.team_size,
        "industry": lead.industry,
        "intent": lead.intent,
        "lead_score": lead.lead_score,
        "status": lead.status.value if lead.status else None,
        "source": lead.source,
        "utm": lead.utm,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


def _extract_crm_id(resp) -> str | None:
    """Best-effort: pull an id-ish value from a 2xx JSON body. Never raises."""
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in _CRM_ID_KEYS:
        if data.get(key) is not None:
            return str(data[key])
    return None


async def sync_lead_to_crm(ctx, notification_log_id: str) -> None:
    """POST a sales-ready lead payload to the tenant's CRM webhook.

    Idempotent: exits early if the log is missing or already SENT.
    Status-based retry (mirrors send_slack_notification):
      - 2xx  → SENT (best-effort Lead.crm_id write-back from the response JSON).
      - 5xx or network/timeout → FAILED + raise _EmailSendFailed (ARQ retries).
      - 4xx  → FAILED, return (permanent client error, no retry).
      - no webhook URL configured → FAILED, return (no raise).

    The webhook URL and the PII payload are NEVER written to the log.
    """
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id))
        if log is None or log.status == NotifStatus.SENT:
            return

        cfg = db.execute(
            select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == log.tenant_id)
        ).scalar_one_or_none()
        url = cfg.crm_webhook_url if cfg else None

        if not url:
            log.status = NotifStatus.FAILED
            log.error = "no crm webhook configured"
            db.commit()
            return

        lead = db.get(Lead, log.entity_id)
        if lead is None:
            log.status = NotifStatus.FAILED
            log.error = "lead not found"
            db.commit()
            return

        try:
            ensure_safe_outbound_url(url)
        except ValueError:
            log.status = NotifStatus.FAILED
            log.error = "blocked webhook host"
            db.commit()
            return

        payload = _crm_payload(lead)
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(url, json=payload)
        except Exception as exc:  # network / timeout — transient, retry
            log.status = NotifStatus.FAILED
            log.error = f"crm network error: {type(exc).__name__}"
            db.commit()
            raise _EmailSendFailed("crm network error") from exc

        if 200 <= resp.status_code < 300:
            crm_id = _extract_crm_id(resp)
            if crm_id:
                lead.crm_id = crm_id
            log.status = NotifStatus.SENT
            db.commit()
            return

        if resp.status_code >= 500:
            log.status = NotifStatus.FAILED
            log.error = f"crm {resp.status_code}"
            db.commit()
            raise _EmailSendFailed(f"crm {resp.status_code}")  # 5xx → ARQ retry

        # 4xx — permanent client error, mark failed, do not retry
        log.status = NotifStatus.FAILED
        log.error = f"crm {resp.status_code}"
        db.commit()

    except _EmailSendFailed:
        raise  # already persisted FAILED; skip fallback
    except Exception as exc:  # noqa: BLE001
        logger.error(f"sync_lead_to_crm failed: {exc}")
        raise
    finally:
        db.close()
