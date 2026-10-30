"""From wanted items to a human (spec 8): cart, interests, catalog quote. Money lives here.

Rules that keep prices honest:
- a cart line is resolved from a row read now (after the reply), never from model text or from the
  block built before the reply, and only for items injected this turn that are still sellable;
- the quote is `Σ qty × price` in integers, and only when every line is `fixed` and no offer is
  pending; a `from` or `on_request` line, or a customer's counter-offer, means a human prices it;
- the customer's own figure (`offered_price`) is stored, never agreed to (decision D1).
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.offering import (
    LeadOfferingInterest,
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


@dataclass
class CartResolution:
    lines: list[ResolvedLine] = field(default_factory=list)
    offered: ResolvedOffer | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.lines) or self.offered is not None


def resolve_cart(
    db: Session, tenant_id: UUID, signals: dict | None, grounding: GroundingResult
) -> CartResolution:
    parsed = parse_catalog_signals(signals)
    injected = {i.seq for i in grounding.items}
    wanted = [line.seq for line in parsed.cart if line.seq in injected]
    if parsed.offered_price and parsed.offered_price.seq in injected:
        wanted.append(parsed.offered_price.seq)
    rows = {r.seq: r for r in _by_seqs(db, tenant_id, wanted)}  # tenant-filtered, active only

    out = CartResolution()
    for line in parsed.cart:
        row = rows.get(line.seq)
        if row is None or row.availability == OfferingAvailability.OUT_OF_STOCK:
            continue
        out.lines.append(ResolvedLine(facts_from_row(row), line.qty))
    offer = parsed.offered_price
    if offer is not None and offer.seq in rows:
        out.offered = ResolvedOffer(facts_from_row(rows[offer.seq]), offer.amount)
    return out


# -- the catalog quote -------------------------------------------------------------------------


def catalog_quote(lines: list[ResolvedLine], currency: str) -> dict | None:
    """`meta.quote` shape, built from live rows; None unless every line has a fixed price."""
    if not lines:
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
    has_offer: bool = False
    has_interests: bool = False


def _scope(lead: Lead):
    return (
        LeadOfferingInterest.tenant_id == lead.tenant_id,
        LeadOfferingInterest.lead_id == lead.id,
    )


def sync_interests(db: Session, lead: Lead, res: CartResolution) -> InterestSync:
    """The lead's wanted items = the latest non-empty cart (earlier offers carry over)."""
    existing = list(db.execute(select(LeadOfferingInterest).where(*_scope(lead))).scalars())
    out = InterestSync(has_offer=res.offered is not None, has_interests=bool(existing))
    if not res.has_content:
        return out

    prior_offer = {r.offering_id: r.offered_price for r in existing if r.offering_id}
    prior_qty = {r.offering_id: r.qty for r in existing if r.offering_id}
    offered_oid = res.offered.facts.id if res.offered else None

    if res.lines:  # a cart replaces the selection
        wanted = {line.facts.id: (line.facts, line.qty) for line in res.lines}
        if res.offered and offered_oid not in wanted:
            wanted[offered_oid] = (res.offered.facts, prior_qty.get(offered_oid, 1))
        db.execute(delete(LeadOfferingInterest).where(*_scope(lead)))
    else:  # an offer alone touches only its own item
        wanted = {offered_oid: (res.offered.facts, prior_qty.get(offered_oid, 1))}
        db.execute(
            delete(LeadOfferingInterest).where(
                *_scope(lead), LeadOfferingInterest.offering_id == offered_oid
            )
        )

    for oid, (facts, qty) in wanted.items():
        offered = res.offered.amount if oid == offered_oid else prior_offer.get(oid)
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
            )
        )
    db.commit()

    if res.offered is not None:
        f = res.offered.facts
        below = f.price is None or res.offered.amount < f.price
        insisted = prior_offer.get(f.id) is not None
        out.handover = below and (f.negotiable or insisted)
    out.first_cart = not existing
    out.has_interests = True
    return out
