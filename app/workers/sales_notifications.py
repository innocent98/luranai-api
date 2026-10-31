"""ARQ jobs that deliver sales notification emails, Slack messages, and CRM webhooks (Stage 3 Slice 5a/5d/5e)."""

import logging
import uuid

from sqlalchemy import select

from app.db.session import SessionLocal
from app.db.models.conversation import ConversationSession
from app.db.models.lead import Lead
from app.db.models.meeting import Meeting
from app.db.models.quote import Quote
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.visitor import Visitor
from app.db.models.tenant import Tenant
from app.services.email_service import EmailService, header_text
from app.services.entitlements import tenant_has
from app.sales.links import lead_dashboard_url
from app.services.quote_pdf_service import format_amount, render_quote_pdf
from app.utils.pinned_http import ResponseRejectedError, UnsafeOutboundURLError, apinned_request

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


def _lead_id_for(db, log: NotificationLog):
    """The lead a notification is about, whatever its entity (lead, meeting or quote)."""
    if log.entity_type == "lead":
        return log.entity_id
    if log.entity_type == "meeting":
        m = db.get(Meeting, log.entity_id)
        return m.lead_id if m else None
    q = db.get(Quote, log.entity_id)
    return q.lead_id if q else None


_CHANNEL_LABELS = {
    "widget": "Website chat",
    "whatsapp": "WhatsApp",
    "direct": "Direct chat link",
    "explore_personal": "Luran Explore",
    "explore_browse": "Luran Explore",
    "explore_search": "Luran Explore",
}


def _entity_context(db, log: NotificationLog) -> dict:
    """Build the template context for the rep alert from the log's entity."""
    context = _entity_fields(db, log)
    context["dashboard_url"] = lead_dashboard_url(_lead_id_for(db, log))
    return context


def _quote_items(quote: Quote | None) -> list[dict]:
    """The quote's lines, formatted the way the PDF prints them."""
    if quote is None:
        return []
    return [
        {
            "label": li.get("label", ""),
            "detail": li.get("detail", ""),
            "amount_fmt": format_amount(int(li.get("amount", 0)), quote.currency),
            "negative": int(li.get("amount", 0)) < 0,
        }
        for li in (quote.line_items or [])
    ]


def _lead_contact(db, lead: Lead | None) -> dict:
    """Who the visitor is and how to reach them. Keys are absent when nothing was captured."""
    if lead is None:
        return {}
    contact: dict = {}
    if lead.name:
        contact["name"] = lead.name
    if lead.company:
        contact["company"] = lead.company
    if lead.email:
        contact["email"] = lead.email
    session = db.get(ConversationSession, lead.conversation_id) if lead.conversation_id else None
    if session is not None:
        visitor = db.get(Visitor, session.visitor_id) if session.visitor_id else None
        if visitor is not None and visitor.phone:
            contact["phone"] = visitor.phone
        if session.source and session.source != "unknown":
            contact["channel"] = _CHANNEL_LABELS.get(session.source, "Chat")
    return contact


def _entity_fields(db, log: NotificationLog) -> dict:
    quote: Quote | None = None
    if log.entity_type == "lead":
        lead = db.get(Lead, log.entity_id)
        fields = {"kind": "lead", "score": lead.lead_score if lead else None}
    elif log.entity_type == "meeting":
        m = db.get(Meeting, log.entity_id)
        lead = db.get(Lead, m.lead_id) if m else None
        fields = {"kind": "meeting", "scheduled_at": m.scheduled_at if m else None}
    else:  # default: quote
        q = quote = db.get(Quote, log.entity_id)
        lead = db.get(Lead, q.lead_id) if q else None
        fields = {
            "kind": "quote",
            "total": q.total if q else None,
            "currency": q.currency if q else None,
            "total_fmt": format_amount(int(q.total or 0), q.currency) if q else None,
        }
    # Existing keys stay (None when unknown); the contact details and items are additive.
    fields.update(name=None, company=None)
    fields.update(_lead_contact(db, lead))
    if quote is None and lead is not None and lead.quote_id:
        quote = db.get(Quote, lead.quote_id)  # a lead/meeting alert shows what they asked for
    fields["items"] = _quote_items(quote)
    return fields


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


def _visitor_quote_text(
    *, tenant: str, greeting: str, items: list[dict], total_fmt: str, has_pdf: bool
) -> str:
    """Plain-text alternative of the visitor email (same facts as the HTML)."""
    lines = [
        f"{it['label']}{' (' + it['detail'] + ')' if it['detail'] else ''}: {it['amount_fmt']}"
        for it in items
    ]
    return "\n".join(
        [
            greeting,
            "",
            f"Here is the quote you asked for in your chat with {tenant}.",
            "",
            *lines,
            f"Estimated total: {total_fmt}",
            "",
            ("The same quote is attached as a PDF. " if has_pdf else "")
            + f"Reply to this email to reach {tenant}.",
            "",
            f"You received this because this address was entered in a chat with {tenant}. "
            "If that wasn't you, you can ignore this email.",
        ]
    )


async def send_visitor_quote_email(ctx, notification_log_id: str) -> None:
    """Email the quote (body + PDF) to the visitor who asked for it.

    Idempotent: exits early if the log is missing or already SENT; the log row is locked while
    sending so a redelivered job waits and then sees SENT. FAILED + re-raise lets ARQ retry. The
    send is plain on purpose: the tenant's name, the quote, a reply-to the rep, nothing else.
    """
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id), with_for_update=True)
        if log is None or log.status == NotifStatus.SENT:
            return
        quote = db.get(Quote, log.entity_id)
        lead = db.get(Lead, quote.lead_id) if quote else None
        tenant = db.get(Tenant, log.tenant_id)
        if quote is None or tenant is None:
            log.status = NotifStatus.FAILED
            log.error = "quote or tenant not found"
            db.commit()
            return
        cfg = db.execute(
            select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == log.tenant_id)
        ).scalar_one_or_none()
        from app.services.notification_service import NotificationService

        reply_to = NotificationService(db)._rep_recipient(log.tenant_id, cfg)
        company = header_text(tenant.display_name or tenant.name or "us")
        items = _quote_items(quote)
        total_fmt = format_amount(int(quote.total or 0), quote.currency)
        # No visitor-typed name: whoever used the chat chose it, the recipient may be a stranger.
        greeting = "Hello,"

        pdf_bytes = None
        try:
            pdf_bytes = render_quote_pdf(quote, lead=lead)
        except Exception as pdf_exc:  # noqa: BLE001 - the body already carries the quote
            logger.warning(f"visitor quote PDF render failed (sending without it): {pdf_exc}")
        ok = EmailService().send_templated_email_with_attachments(
            to_email=log.recipient,
            subject=f"Your quote from {company}",
            template_name="sales_quote_visitor.html",
            context={
                "company": company,
                "greeting": greeting,
                "items": items,
                "total_fmt": total_fmt,
                "has_pdf": bool(pdf_bytes),
            },
            attachments=(
                [(f"quote-{quote.id}.pdf", pdf_bytes, "application/pdf")] if pdf_bytes else None
            ),
            text_content=_visitor_quote_text(
                tenant=company,
                greeting=greeting,
                items=items,
                total_fmt=total_fmt,
                has_pdf=bool(pdf_bytes),
            ),
            reply_to=reply_to,
            from_name=f"{company} via Luran",  # the sender is the platform, not the tenant
        )
        if not ok:
            log.status = NotifStatus.FAILED
            log.error = "send_templated_email returned False"
            db.commit()
            raise _EmailSendFailed("email send failed")
        log.status = NotifStatus.SENT
        log.error = None
        db.commit()
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, _EmailSendFailed):
            raise
        logger.error(f"send_visitor_quote_email failed: {exc}")
        try:
            db.rollback()
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


def _slack_escape(text: str) -> str:
    """Slack treats & < > as control characters (links, @channel mentions): escape them."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_body(db, log: NotificationLog) -> str:
    """The Slack message body: the log's one-liner plus who to contact and what they asked for.

    Every visitor-supplied value is escaped, so a name like `<!channel>` cannot ping the room.
    """
    f = _entity_fields(db, log)
    lines = [_slack_escape(log.body or "")]
    for label, key in (
        ("Name", "name"),
        ("Email", "email"),
        ("Phone", "phone"),
        ("Channel", "channel"),
    ):
        if f.get(key):
            lines.append(f"{label}: {_slack_escape(str(f[key]))}")
    if f.get("total_fmt"):
        lines.append(f"Total: {_slack_escape(f['total_fmt'])}")
    for it in f.get("items") or []:
        lines.append(f"• {_slack_escape(str(it['label']))}: {_slack_escape(it['amount_fmt'])}")
    return "\n".join(line for line in lines if line)


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

        if not tenant_has(db, log.tenant_id, "crm_notifications"):
            # Queued before a downgrade: the plan no longer includes Slack, so do not send.
            log.status = NotifStatus.FAILED
            log.error = "plan does not include slack notifications"
            db.commit()
            return

        if not url:
            log.status = NotifStatus.FAILED
            log.error = "no slack webhook configured"
            db.commit()
            return

        dashboard_url = lead_dashboard_url(_lead_id_for(db, log)) or ""
        try:
            resp = await apinned_request(
                "POST",
                url,
                json=_slack_blocks(log.title, _slack_body(db, log), dashboard_url),
                timeout=10,
            )
        except UnsafeOutboundURLError:
            log.status = NotifStatus.FAILED
            log.error = "blocked webhook host"
            db.commit()
            return
        except ResponseRejectedError:
            # The endpoint answered with something we refuse to buffer: retrying
            # would get the same answer, so fail permanently (no ARQ retry).
            log.status = NotifStatus.FAILED
            log.error = "response rejected"
            db.commit()
            return
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

        if not tenant_has(db, log.tenant_id, "crm_notifications"):
            log.status = NotifStatus.FAILED
            log.error = "plan does not include crm notifications"
            db.commit()
            return

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

        payload = _crm_payload(lead)
        try:
            resp = await apinned_request("POST", url, json=payload, timeout=10)
        except UnsafeOutboundURLError:
            log.status = NotifStatus.FAILED
            log.error = "blocked webhook host"
            db.commit()
            return
        except ResponseRejectedError:
            # The endpoint answered with something we refuse to buffer: retrying
            # would get the same answer, so fail permanently (no ARQ retry).
            log.status = NotifStatus.FAILED
            log.error = "response rejected"
            db.commit()
            return
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
