"""Email the quote to the visitor who asked for it, within strict abuse limits.

The visitor typed their address into a public chat, so anyone can type anyone's address. Every
send therefore passes, in order: the kill switch, a valid address, a reply-to for the tenant,
one email per quote, a per-tenant daily cap, a per-address 24 h cap across all tenants, then a
per-conversation and a per-IP cap. Only then is a log row written and an ARQ job queued (the
worker does the sending). The caller shows the visitor what happened, in code-written words.

The per-tenant and per-address caps count `notification_logs` rows, so they survive a Redis
flush and cannot fail open; the per-conversation and per-IP caps use the shared Redis limiter.
"""

import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.lead import Lead
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifEvent, NotifStatus
from app.db.models.quote import Quote
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.sales.quote_email import SentOutcome, finalize_reply, mask_email
from app.services import notification_enqueue
from app.services.notification_service import NotificationService
from app.utils.http_rate_limiter import (
    enforce_identifier_limit,
    quote_email_ip_rate_limiter,
    quote_email_session_rate_limiter,
)

logger = logging.getLogger(__name__)

JOB_NAME = "send_visitor_quote_email"
_WINDOW = timedelta(hours=24)
_PLUS_TAG = re.compile(r"\+[^@]*(?=@)")
_COUNTED = (NotifStatus.PENDING, NotifStatus.SENT)  # a failed send never reached the inbox

Enqueue = Callable[[list[tuple[uuid.UUID, str]]], Awaitable[None]]


def _normalise(address: str) -> str:
    return address.strip().lower()


_GMAIL = ("gmail.com", "googlemail.com")


def recipient_key(address: str) -> str:
    """Stable, address-free key for the per-address cap: case and `+tag` are ignored so
    `victim+1@x.com`, `victim+2@x.com` and `Victim@x.com` are one inbox; Gmail ignores dots and
    treats googlemail.com as gmail.com, so those are folded too."""
    local, _, domain = _PLUS_TAG.sub("", _normalise(address)).partition("@")
    if domain in _GMAIL:
        local, domain = local.replace(".", ""), "gmail.com"
    return hashlib.sha256(f"{local}@{domain}".encode()).hexdigest()


def _lock(db: Session, name: str) -> None:
    """Transaction-scoped advisory lock (Postgres): serialises check-then-insert for one tenant
    or one address, so two concurrent requests cannot both pass a cap's count. Released by the
    next commit. Other databases (the SQLite test job) have no concurrent writers to guard."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:n, 0))"), {"n": name})


def _valid_address(address: str | None) -> str | None:
    if not address or len(address) > 254:
        return None
    try:
        return validate_email(address.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def _reply_to(db: Session, tenant_id: uuid.UUID) -> str | None:
    """Who the visitor's reply reaches: the same address the rep alerts go to."""
    config = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
    ).scalar_one_or_none()
    return NotificationService(db)._rep_recipient(tenant_id, config)


def can_email_quote(db: Session, tenant_id: uuid.UUID) -> bool:
    """Whether a quote for this tenant could be emailed if the visitor gave an address."""
    return bool(settings.QUOTE_VISITOR_EMAIL_ENABLED and _reply_to(db, tenant_id))


def _same_quote_already_sent(db: Session, quote: Quote) -> bool:
    """The model may offer the same quote on several turns; the visitor gets it once. A quote
    that differs (other plan, team size, items) is new and is sent."""
    earlier = db.execute(
        select(Quote)
        .join(NotificationLog, NotificationLog.entity_id == Quote.id)
        .where(
            Quote.lead_id == quote.lead_id,
            Quote.id != quote.id,
            NotificationLog.event == NotifEvent.QUOTE_EMAILED_VISITOR,
            NotificationLog.status.in_(_COUNTED),
        )
    ).scalars()
    return any(
        e.total == quote.total and e.currency == quote.currency and e.line_items == quote.line_items
        for e in earlier
    )


def _count(db: Session, *conditions) -> int:
    since = datetime.now(UTC) - _WINDOW
    return db.execute(
        select(func.count())
        .select_from(NotificationLog)
        .where(
            NotificationLog.event == NotifEvent.QUOTE_EMAILED_VISITOR,
            NotificationLog.status.in_(_COUNTED),
            NotificationLog.created_at >= since,
            *conditions,
        )
    ).scalar_one()


async def _limited(limiter, identifier: str) -> bool:
    try:
        await enforce_identifier_limit(limiter, identifier)
    except HTTPException:
        return True
    return False


async def email_quote_to_visitor(
    db: Session,
    quote: Quote,
    *,
    client_ip: str | None = None,
    enqueue: Enqueue | None = None,
) -> SentOutcome | None:
    """Queue the visitor's copy of `quote`.

    Returns `SentOutcome.queued(masked)` when a send is queued, `SentOutcome.capped()` when a
    limit stopped it (worth telling the visitor), and None when there is nothing to send or
    nothing to say (switch off, no usable address, nobody to reply to, already sent).
    """
    if not settings.QUOTE_VISITOR_EMAIL_ENABLED:
        return None
    enqueue = enqueue or notification_enqueue.enqueue_jobs_async
    lead = db.get(Lead, quote.lead_id)
    address = _valid_address(lead.email if lead else None)
    if address is None or _reply_to(db, quote.tenant_id) is None:
        return None
    already = db.execute(
        select(NotificationLog.id).where(
            NotificationLog.event == NotifEvent.QUOTE_EMAILED_VISITOR,
            NotificationLog.entity_id == quote.id,
        )
    ).first()
    if already is not None or _same_quote_already_sent(db, quote):
        return None

    key = recipient_key(address)
    _lock(db, f"quote-email-tenant:{quote.tenant_id}")  # fixed order: tenant, then address
    _lock(db, f"quote-email-address:{key}")
    if (
        _count(db, NotificationLog.tenant_id == quote.tenant_id)
        >= settings.QUOTE_VISITOR_EMAIL_TENANT_DAILY_CAP
        or _count(db, NotificationLog.body == key) >= settings.QUOTE_VISITOR_EMAIL_RECIPIENT_CAP_24H
    ):
        logger.info("visitor quote email capped (tenant or address) for tenant %s", quote.tenant_id)
        db.commit()  # release the advisory locks
        return SentOutcome.capped()
    session_id = str(lead.conversation_id or quote.lead_id)
    if await _limited(quote_email_session_rate_limiter, session_id) or (
        client_ip and await _limited(quote_email_ip_rate_limiter, client_ip)
    ):
        logger.info("visitor quote email capped (session or ip) for tenant %s", quote.tenant_id)
        db.commit()  # release the advisory locks
        return SentOutcome.capped()

    log_id = NotificationService(db)._try_log(
        tenant_id=quote.tenant_id,
        event=NotifEvent.QUOTE_EMAILED_VISITOR,
        channel=NotifChannel.EMAIL,
        entity_type="quote",
        entity_id=quote.id,
        recipient=address,
        title="Quote emailed to visitor",
        body=key,  # address-free handle the per-address cap counts on
    )
    db.commit()  # releases the locks: the next request counts this row
    if log_id is None:
        return None
    try:
        await enqueue([(log_id, JOB_NAME)])
    except Exception:  # noqa: BLE001 - Redis down: no job exists, so promise nothing
        logger.exception("visitor quote email enqueue failed")
        log = db.get(NotificationLog, log_id)
        if log is not None:
            log.status = NotifStatus.FAILED
            log.error = "enqueue failed"
            db.commit()
        return None
    return SentOutcome.queued(mask_email(address))


async def outcome_for_turn(
    db: Session,
    *,
    lead: Lead | None,
    new_quote: Quote | None,
    email_just_captured: bool,
    client_ip: str | None = None,
    channel: str | None = None,
) -> SentOutcome | None:
    """What the visitor should be told about emailing the quote after this chat turn.

    A quote is emailed when it is generated for a lead that already has an address, or when the
    address first arrives after a quote was shown. When the visitor has no address yet, a quote
    generated this turn adds a one-line invitation, but only where a send could really happen
    (switch on, a reply-to exists) and not on WhatsApp, where an email is rarely captured.
    """
    if lead is None:
        return None
    quote = new_quote
    if quote is None and email_just_captured and lead.quote_id:
        quote = db.get(Quote, lead.quote_id)
    if quote is None:
        return None
    if _valid_address(lead.email):
        return await email_quote_to_visitor(db, quote, client_ip=client_ip, enqueue=None)
    if new_quote is not None and channel != "whatsapp" and can_email_quote(db, lead.tenant_id):
        return SentOutcome.invite()
    return None


async def finalize_turn_reply(
    db: Session,
    reply: str,
    *,
    lead: Lead | None,
    new_quote: Quote | None,
    email_just_captured: bool,
) -> str:
    """The chat handler's one call: email the quote when due and return the reply the visitor
    sees (model-made email promises removed, the true sentence added). Never raises: a failure
    here must cost the visitor the note, not the answer."""
    from app.services.conversation_service import message_channel
    from app.utils.http_rate_limiter import request_client_ip

    outcome = None
    try:
        outcome = await outcome_for_turn(
            db,
            lead=lead,
            new_quote=new_quote,
            email_just_captured=email_just_captured,
            client_ip=request_client_ip.get(),
            channel=message_channel.get(),
        )
    except Exception:  # noqa: BLE001
        logger.exception("quote email step failed (non-fatal)")
    return finalize_reply(reply, outcome)
