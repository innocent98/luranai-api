"""Side effects of a catalog turn, called from `send_message` after the reply is final.

Everything here is best effort around the reply (the caller wraps it like the other sales steps and
rolls the session back on failure): a failure is logged and the customer still gets their answer.
Every sentence that mentions a customer's offer, a dropped item or a price a human must confirm is
written here, by code, never by the model.
"""

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.notification_log import NotifEvent
from app.db.models.quote import Quote
from app.offerings.alerts import interest_lines
from app.db.models.offering import LeadOfferingInterest, OfferingAvailability
from app.offerings.cart import (
    CartResolution,
    InterestSync,
    ResolvedLine,
    catalog_quote,
    quote_too_large,
    sync_interests,
)
from app.offerings.facts import facts_from_row
from app.offerings.grounding import _by_seqs
from app.offerings.facts import format_amount
from app.services.notification_enqueue import enqueue_jobs_async
from app.services.notification_service import NotificationService
from app.services.quote_service import QuoteService

logger = logging.getLogger(__name__)

PRICE_CONFIRM_LINE = "A team member will confirm the price."
TOO_LARGE_LINE = "This order is too large for me to total; a team member will confirm the price."
HANDOVER_LINE = "I'm handing this to the team now; they'll take it from here."
_OFFER_NS = uuid.UUID("5d1c6d1e-2f0b-4c57-9a52-0f7f6f0c0ffe")

_DROPPED_WORDING = {
    "unknown": "isn't something I can find in this chat's list",
    "gone": "isn't in the current list any more",
    "sold_out": "is sold out",
}


def lock_lead(db: Session, lead: Lead) -> None:
    """Serialise the T6 block per lead: two parallel turns must not double the wanted items."""
    db.execute(select(Lead.id).where(Lead.id == lead.id).with_for_update())


def offer_key(lead_id: uuid.UUID, offering_id: uuid.UUID, amount: int) -> uuid.UUID:
    """Alert key per lead, item and amount: a later or different offer is never swallowed."""
    return uuid.uuid5(_OFFER_NS, f"{lead_id}:{offering_id}:{amount}")


async def _notify(
    db: Session,
    tenant_id,
    event: NotifEvent,
    entity_type: str,
    entity_id,
    *,
    text: tuple[str, str] | None = None,
    crm: bool = True,
) -> None:
    try:
        jobs = NotificationService(db).dispatch(
            tenant_id, event, entity_type=entity_type, entity_id=entity_id, text=text, crm=crm
        )
        await enqueue_jobs_async(jobs)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("offerings notification dispatch failed (non-fatal): %s", event.value)


def record_cart(db: Session, lead: Lead, res: CartResolution) -> InterestSync:
    """Write and commit the lead's wanted items (a later failure must never take them back)."""
    return sync_interests(db, lead, res)


def catalog_score_signals(db: Session, lead: Lead) -> dict:
    """Display signals for the lead list, read from the stored items so they never bounce."""
    rows = db.execute(
        select(LeadOfferingInterest.offered_price).where(
            LeadOfferingInterest.tenant_id == lead.tenant_id,
            LeadOfferingInterest.lead_id == lead.id,
        )
    ).all()
    return {
        "selected_items": bool(rows),
        "price_offer": any(r[0] is not None for r in rows),
    }


async def alert_owner(
    db: Session, lead: Lead, synced: InterestSync, res: CartResolution, currency: str
) -> None:
    """The first committed cart, and every offer that hands over (new item, amount or lead)."""
    if synced.first_cart:
        await _notify(db, lead.tenant_id, NotifEvent.OFFERING_REQUEST, "lead", lead.id)
    if res.offered is not None and synced.handover and synced.offer_changed:
        item, amount = res.offered.facts, res.offered.amount
        who = lead.name or "A customer"
        body = (
            f"{who} offered {format_amount(amount, currency)} for {item.name} "
            f"(listed {item.price_text(currency)}). "
            + "; ".join(interest_lines(db, lead.tenant_id, lead.id)[:5])
        )
        await _notify(
            db,
            lead.tenant_id,
            NotifEvent.OFFERING_REQUEST,
            "offering_offer",
            offer_key(lead.id, item.id, amount),
            text=("Price offer", body),
            crm=False,
        )


@dataclass
class TurnNotes:
    """Sentences code adds after the model's reply, and whether a human takes over now."""

    lines: list[str] = field(default_factory=list)
    handover_now: bool = False


def reply_notes(res: CartResolution, outcome: "QuoteOutcome") -> TurnNotes:
    """Dropped lines, an order too large to total, a price a team member must confirm."""
    notes = TurnNotes()
    for d in res.dropped:
        label = d.name or f"#{d.seq}"
        notes.lines.append(f"I couldn't add {label}: it {_DROPPED_WORDING[d.reason]}.")
    if outcome.overflow:
        notes.lines.append(TOO_LARGE_LINE)
        notes.handover_now = True
    elif outcome.note:
        notes.lines.append(outcome.note)
        notes.handover_now = outcome.handover
    if notes.handover_now:
        notes.lines.append(HANDOVER_LINE)
    return notes


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


@dataclass
class QuoteOutcome:
    quote: dict | None = None
    created: object | None = None  # the stored Quote row (the visitor's quote email needs it)
    note: str | None = None  # a sentence code adds when the customer should hear why
    overflow: bool = False
    handover: bool = False


def stored_lines(db: Session, lead: Lead) -> list[ResolvedLine] | None:
    """The quote is built from what is stored at write time (quantities from the lead's rows,
    prices from the live items), so parallel turns can never leave it out of step. None when a
    stored item can no longer be sold."""
    rows = list(
        db.execute(
            select(LeadOfferingInterest).where(
                LeadOfferingInterest.tenant_id == lead.tenant_id,
                LeadOfferingInterest.lead_id == lead.id,
            )
        ).scalars()
    )
    seqs = [int(r.ref.lstrip("#")) for r in rows if r.ref.lstrip("#").isdigit()]
    live = {o.seq: o for o in _by_seqs(db, lead.tenant_id, seqs)}
    lines = []
    for r in rows:
        row = live.get(int(r.ref.lstrip("#"))) if r.ref.lstrip("#").isdigit() else None
        if row is None or row.availability == OfferingAvailability.OUT_OF_STOCK:
            return None
        lines.append(ResolvedLine(facts_from_row(row), r.qty))
    return lines


async def issue_quote(
    db: Session, lead: Lead, res: CartResolution, synced: InterestSync, currency: str
) -> QuoteOutcome:
    """The catalog quote for the lead's stored selection, or none (and why): a line that could not
    be resolved, an offer a human is working on, a total the quote column cannot hold, a line
    that is not fixed-price, or an unchanged selection."""
    if res.dropped or not res.lines:
        return QuoteOutcome()  # the selection is not trustworthy yet; a human confirms
    if synced.pending_offer:
        return QuoteOutcome(note=PRICE_CONFIRM_LINE, handover=True)
    lines = stored_lines(db, lead)
    if not lines:
        return QuoteOutcome()
    if quote_too_large(lines):
        return QuoteOutcome(overflow=True)
    quote = catalog_quote(lines, currency)
    if quote is None or _same_as_latest(db, lead, quote):
        return QuoteOutcome()
    created = QuoteService(db).create_quote(
        lead.tenant_id,
        lead,
        currency=quote["currency"],
        line_items=quote["line_items"],
        total=quote["total"],
    )
    await _notify(db, lead.tenant_id, NotifEvent.QUOTE_GENERATED, "quote", created.id)
    return QuoteOutcome(quote=quote, created=created)
