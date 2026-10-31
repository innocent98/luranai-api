"""From wanted items to a human (spec 8): cart, interests, catalog quote. Money lives here.

Rules that keep prices honest:
- a cart line is resolved from a row read now (after the reply), never from model text or from the
  block built before the reply, and only for items this session has seen (injected this turn,
  shown earlier, or already wanted) that are still sellable; a line that cannot be resolved is
  reported, never silently dropped;
- the quote is `Σ qty × price` in integers, and only when every line is `fixed` and no offer is
  pending; a `from` or `on_request` line, or a customer's counter-offer, means a human prices it;
- the customer's own figure (`offered_price`) is stored, never agreed to (decision D1).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession
from app.db.models.lead import Lead
from app.db.models.offering import (
    LeadOfferingInterest,
    Offering,
    OfferingAvailability,
    OfferingPriceMode,
)
from app.offerings.facts import OfferingFacts, facts_from_row, format_amount
from app.offerings.grounding import GroundingResult, _by_seqs
from app.offerings.signals import parse_catalog_signals


@dataclass(frozen=True)
class ResolvedLine:
    facts: OfferingFacts
    qty: int


@dataclass(frozen=True)
class ResolvedOffer:
    facts: OfferingFacts
    amount: int


@dataclass(frozen=True)
class Dropped:
    seq: int
    reason: str  # "unknown" | "gone" | "sold_out"
    name: str | None = None


@dataclass
class CartResolution:
    lines: list[ResolvedLine] = field(default_factory=list)
    offered: ResolvedOffer | None = None
    dropped: list[Dropped] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.lines) or self.offered is not None


def resolve_cart(
    db: Session,
    tenant_id: UUID,
    signals: dict | None,
    grounding: GroundingResult,
    customer_amounts: set | None = None,
    extra_seqs: Iterable[int] = (),
) -> CartResolution:
    parsed = parse_catalog_signals(signals)
    allowed = {i.seq for i in grounding.items} | set(extra_seqs)
    offer = parsed.offered_price
    wanted = [line.seq for line in parsed.cart if line.seq in allowed]
    if offer and offer.seq in allowed:
        wanted.append(offer.seq)
    rows = {r.seq: r for r in _by_seqs(db, tenant_id, wanted)}  # tenant-filtered, active only

    out = CartResolution()
    for line in parsed.cart:
        if line.seq not in allowed:
            out.dropped.append(Dropped(line.seq, "unknown"))
            continue
        row = rows.get(line.seq)
        if row is None:
            out.dropped.append(Dropped(line.seq, "gone"))
        elif row.availability == OfferingAvailability.OUT_OF_STOCK:
            out.dropped.append(Dropped(line.seq, "sold_out", facts_from_row(row).name))
        else:
            out.lines.append(ResolvedLine(facts_from_row(row), line.qty))
    # an offer is the customer's own figure: one they never wrote is the model's invention
    said = customer_amounts is None or (offer is not None and offer.amount in customer_amounts)
    if offer is not None and said:
        row = rows.get(offer.seq)
        if row is None:
            out.dropped.append(
                Dropped(offer.seq, "unknown" if offer.seq not in allowed else "gone")
            )
        elif row.availability == OfferingAvailability.OUT_OF_STOCK:
            out.dropped.append(Dropped(offer.seq, "sold_out", facts_from_row(row).name))
        elif row.price is not None and offer.amount >= row.price:
            pass  # at or above the list price there is nothing to negotiate: a normal cart
        else:
            out.offered = ResolvedOffer(facts_from_row(row), offer.amount)
    return out


# -- the catalog quote -------------------------------------------------------------------------


QUOTE_COLUMN_MAX = 2_147_483_647  # `quotes.total` / line amounts are 32-bit; S1 owns the schema


def quote_too_large(lines: list[ResolvedLine]) -> bool:
    """A line or total the quote column cannot hold: no quote, a human prices it."""
    total = 0
    for line in lines:
        if line.facts.price is None:
            continue
        amount = line.qty * line.facts.price
        total += amount
        if amount > QUOTE_COLUMN_MAX or total > QUOTE_COLUMN_MAX:
            return True
    return False


def catalog_quote(lines: list[ResolvedLine], currency: str) -> dict | None:
    """`meta.quote` shape, built from live rows; None unless every line has a fixed price."""
    if not lines or quote_too_large(lines):
        return None
    line_items = []
    total = 0
    for line in lines:
        f = line.facts
        if f.price_mode != OfferingPriceMode.FIXED or not f.price:
            return None
        amount = line.qty * f.price
        total += amount
        detail = f"{line.qty} × {format_amount(f.price, currency)}"
        if f.price_note:
            detail += f" {f.price_note}"
        line_items.append({"label": f.name, "detail": detail, "amount": amount})
    return {"currency": currency, "line_items": line_items, "total": total}


# -- interests ---------------------------------------------------------------------------------


@dataclass
class InterestSync:
    first_cart: bool = False
    handover: bool = False
    has_offer: bool = False  # the customer made an offer on this turn
    offer_changed: bool = False  # ...and it differs from the one already stored
    pending_offer: bool = False  # the lead carries an unanswered offer (this turn or earlier)
    has_interests: bool = False
    partial: bool = False  # some cart lines could not be resolved


def _scope(lead: Lead):
    return (
        LeadOfferingInterest.tenant_id == lead.tenant_id,
        LeadOfferingInterest.lead_id == lead.id,
    )


def has_pending_offer(db: Session, lead: Lead) -> bool:
    """An offer a human is working on: stored on a negotiable (or unpriced) item, until a human has
    joined the conversation after it was made. An offer on a fixed-price item gets the firm-price
    answer and never blocks a quote."""
    rows = db.execute(
        select(LeadOfferingInterest.id)
        .join(Offering, Offering.id == LeadOfferingInterest.offering_id)
        .join(Lead, Lead.id == LeadOfferingInterest.lead_id)
        .outerjoin(ConversationSession, ConversationSession.id == Lead.conversation_id)
        .where(
            *_scope(lead),
            LeadOfferingInterest.offered_price.is_not(None),
            or_(Offering.negotiable.is_(True), Offering.price.is_(None)),
            # a human who joined the conversation after the offer was made has taken it up: it is
            # no longer pending, so the next cart can be quoted again
            or_(
                ConversationSession.agent_joined_at.is_(None),
                ConversationSession.agent_joined_at < LeadOfferingInterest.created_at,
            ),
        )
        .limit(1)
    ).first()
    return rows is not None


def sync_interests(
    db: Session, lead: Lead, res: CartResolution, *, commit: bool = True
) -> InterestSync:
    """The lead's wanted items = the latest non-empty cart (earlier offers carry over).

    A cart with a line that could not be resolved never replaces the selection: its resolved lines
    are merged into what is already wanted and nothing is deleted. `commit=False` leaves the
    transaction (and the caller's row lock) open.
    """
    existing = list(db.execute(select(LeadOfferingInterest).where(*_scope(lead))).scalars())
    out = InterestSync(
        has_offer=res.offered is not None,
        has_interests=bool(existing),
        partial=bool(res.dropped and res.lines),
    )
    if not res.has_content:
        out.pending_offer = has_pending_offer(db, lead)
        return out

    prior_offer = {r.offering_id: r.offered_price for r in existing if r.offering_id}
    # `created_at` doubles as "when this offer was made": it is carried across the delete and
    # re-insert below and only moves when the offered amount is stored for the first time or
    # changes, so a human who took the offer up stays later than it however often the customer
    # names the item again
    prior_made = {r.offering_id: r.created_at for r in existing if r.offering_id}
    prior_qty = {r.offering_id: r.qty for r in existing if r.offering_id}
    offered_oid = res.offered.facts.id if res.offered else None

    merge = bool(res.dropped) or not res.lines
    wanted: dict = {line.facts.id: (line.facts, line.qty) for line in res.lines}
    if res.offered and offered_oid not in wanted:
        wanted[offered_oid] = (res.offered.facts, prior_qty.get(offered_oid, 1))
    if merge:  # touch only the items named now; everything else already wanted stays
        db.execute(
            delete(LeadOfferingInterest).where(
                *_scope(lead), LeadOfferingInterest.offering_id.in_(list(wanted))
            )
        )
    else:  # a clean cart replaces the selection
        db.execute(delete(LeadOfferingInterest).where(*_scope(lead)))

    for oid, (facts, qty) in wanted.items():
        offered = res.offered.amount if oid == offered_oid else prior_offer.get(oid)
        carried = prior_made.get(oid) if offered == prior_offer.get(oid) else None
        db.add(
            LeadOfferingInterest(
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                offering_id=oid,
                ref=facts.ref,
                name_snapshot=facts.name,
                price_mode_snapshot=facts.price_mode.value,
                price_snapshot=facts.price,
                price_note_snapshot=facts.price_note,
                qty=qty,
                offered_price=offered,
                **({"created_at": carried} if carried is not None else {}),
            )
        )
    db.commit() if commit else db.flush()

    if res.offered is not None:
        f = res.offered.facts
        below = f.price is None or res.offered.amount < f.price
        insisted = prior_offer.get(f.id) is not None
        out.handover = below and (f.negotiable or insisted or f.price is None)
        out.offer_changed = prior_offer.get(f.id) != res.offered.amount
    out.first_cart = not existing
    out.has_interests = True
    out.pending_offer = has_pending_offer(db, lead)
    return out
