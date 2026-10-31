"""Bargaining turns are answered by code, never by the model (decision D1, review S-B).

A turn is an offer turn when the customer's latest message names a figure below the price of the
item under discussion, asks for a percentage, a discount or "the last price", or the model returned
an offered price. The model's text is dropped and the reply is built from the rows: the price is
firm, or the owner is open to offers, or a human takes over. This removes the free text in which an
agreement could hide, whatever words it used.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from app.offerings.amounts import customer_amounts
from app.offerings.bargain import BROWSE, PAYMENT_PLAN, cap, has_bargain_wording
from app.offerings.cart import CartResolution
from app.offerings.facts import OfferingFacts, format_amount
from app.offerings.grounding import GroundingResult

ASK_TEAM = "Would you like me to connect you with the team?"
HANDOVER_LINE = "I'm handing this to the team now; they'll take it from here."


def _matched(grounding: GroundingResult) -> list[OfferingFacts]:
    return [i for i in grounding.items if i.seq in grounding.matched_seqs]


def is_offer_turn(
    message: str,
    grounding: GroundingResult,
    offered_signal: bool,
    context_prices: Callable[[], list[int]] | None = None,
) -> bool:
    """Strong signals only: bargaining wording, or a money figure below the price in play (the item
    this turn matched, else one shown or wanted earlier in the conversation). With no item known
    at all, a figure-only message from a tenant that has items is an offer candidate: a human
    prices it. An ordinary question that merely contains a number is not one."""
    if grounding.strict or grounding.mode == "unavailable":
        return False
    text = (cap([message]) or [""])[0]
    if offered_signal or has_bargain_wording(text):
        return True
    if PAYMENT_PLAN.search(text):
        return False  # "12,500 now and the balance on delivery": a payment plan, not a price
    prices = [i.price for i in _matched(grounding) if i.price is not None]
    if not prices and context_prices is not None:
        prices = [p for p in context_prices() if p is not None]
    if not prices:
        # nothing to compare with: a bare figure from a shop with items is still an offer
        # candidate unless the message is a question or a budget search
        listed = {i.price for i in grounding.items if i.price is not None}
        figures = [a.value for a in customer_amounts(text, small_ok=False)]
        # a figure that is some listed price is the customer quoting the list, not an offer
        return any(v not in listed for v in figures) and not BROWSE.search(text)
    low = min(prices)
    return any(a.value < low for a in customer_amounts(text, small_ok=low < 1000))


@dataclass
class OfferTurn:
    lines: list[str] = field(default_factory=list)
    handover_now: bool = False
    consent: bool = False  # offer the team and wait for a yes (no cart to record against)


def _firm(item: OfferingFacts, currency: str) -> str:
    return f"The listed price for the {item.name} is {item.price_text(currency)} and it is firm."


def compose_offer_turn(
    grounding: GroundingResult,
    cart: CartResolution | None,
    handover: bool,
    currency: str,
    *,
    amount_recorded: bool,
) -> OfferTurn:
    """`handover`: the cart step decided a human must take over (negotiable item below list, an
    insisting customer or an unpriced item). `amount_recorded`: an offer was stored on the lead."""
    offered = cart.offered if cart is not None else None
    target = offered.facts if offered else next(iter(_matched(grounding)), None)
    if target is None:
        return OfferTurn(
            lines=["I can't change prices myself, but the team can discuss it with you.", ASK_TEAM],
            consent=True,
        )
    out = OfferTurn()
    if target.price is None:  # priced by the team on request
        out.lines.append(f"The price for the {target.name} is quoted by the team.")
        out.handover_now = cart is not None
        out.consent = cart is None
    elif offered is not None and amount_recorded and handover:
        out.lines.append(
            f"I've noted your offer of {format_amount(offered.amount, currency)} "
            f"for the {target.name}."
        )
        out.handover_now = True
    elif target.negotiable:
        out.lines.append(
            f"The owner is open to offers on the {target.name}. Tell me the price you have in "
            "mind and I'll pass it to the team."
        )
        if cart is None:  # support mode cannot record an offer: a human has to take it
            out.lines[-1] = f"The owner is open to offers on the {target.name}."
            out.lines.append(ASK_TEAM)
            out.consent = True
    else:
        out.lines.append(_firm(target, currency))
        if handover:
            out.handover_now = True
    if out.handover_now:
        out.lines.append(HANDOVER_LINE)
    return out
