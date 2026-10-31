"""Side effects of a catalog turn, called from `send_message` after the reply is final.

Everything here is best effort around the reply (the caller wraps it like the other sales steps):
a failure is logged and the customer still gets their answer.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.notification_log import NotifEvent
from app.db.models.quote import Quote
from app.offerings.cart import CartResolution, InterestSync, catalog_quote, sync_interests
from app.offerings.facts import format_amount
from app.services.notification_enqueue import enqueue_jobs_async
from app.services.notification_service import NotificationService
from app.services.quote_service import QuoteService

logger = logging.getLogger(__name__)


async def _notify(db: Session, tenant_id, event: NotifEvent, entity_type: str, entity_id) -> None:
    try:
        jobs = NotificationService(db).dispatch(
            tenant_id, event, entity_type=entity_type, entity_id=entity_id
        )
        await enqueue_jobs_async(jobs)
    except Exception:  # noqa: BLE001
        logger.exception("offerings notification dispatch failed (non-fatal): %s", event.value)


def record_cart(db: Session, lead: Lead, res: CartResolution) -> InterestSync:
    """Write the lead's wanted items (the lead is enriched next, then the owner is alerted)."""
    return sync_interests(db, lead, res)


async def alert_owner(db: Session, lead: Lead, synced: InterestSync) -> None:
    """Once, on the first committed cart; after enrichment so the alert carries the name."""
    if synced.first_cart:
        await _notify(db, lead.tenant_id, NotifEvent.OFFERING_REQUEST, "lead", lead.id)


def offer_ack(res: CartResolution, currency: str, *, handover: bool) -> str | None:
    """The only sentences that mention a customer's price offer, written by code (never the model).

    "Noted your offer" is only said when a human will pick it up (negotiable item, or the customer
    insists); otherwise code states that the listed price is firm.
    """
    if res.offered is None:
        return None
    item, amount = res.offered.facts, res.offered.amount
    if handover:
        return f"I've noted your offer of {format_amount(amount, currency)} for the {item.name}."
    if item.price is not None and amount < item.price:
        return (
            f"The listed price for the {item.name} is {format_amount(item.price, currency)} "
            "and it is firm."
        )
    return None


def catalog_score_signals(synced: InterestSync) -> dict:
    """Display signals for the lead list (they appear in the breakdown only when fired)."""
    return {"selected_items": synced.has_interests, "price_offer": synced.has_offer}


def _same_as_latest(db: Session, lead: Lead, quote: dict) -> bool:
    latest = (
        db.execute(
            select(Quote)
            .where(Quote.tenant_id == lead.tenant_id, Quote.lead_id == lead.id)
            .order_by(Quote.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return bool(
        latest
        and latest.total == quote["total"]
        and latest.currency == quote["currency"]
        and latest.line_items == quote["line_items"]
    )


async def issue_quote(
    db: Session, lead: Lead, res: CartResolution, synced: InterestSync, currency: str
) -> dict | None:
    """The catalog quote for this cart, or None (not all fixed, an offer pending, or unchanged)."""
    if synced.has_offer or not res.lines:
        return None  # a counter-offer means a human prices it
    quote = catalog_quote(res.lines, currency)
    if quote is None or _same_as_latest(db, lead, quote):
        return None
    created = QuoteService(db).create_quote(
        lead.tenant_id,
        lead,
        currency=quote["currency"],
        line_items=quote["line_items"],
        total=quote["total"],
    )
    await _notify(db, lead.tenant_id, NotifEvent.QUOTE_GENERATED, "quote", created.id)
    return quote
