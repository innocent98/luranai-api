"""The reply code builds when the guard has rejected the model twice (spec 7.5, 14).

Everything printed here comes from the rows read this turn: no model text, no computed figure.
"""

from app.db.models.offering import OfferingAvailability, OfferingKind
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult

ASK_TEAM = "Would you like me to connect you with the team?"


def _availability(item: OfferingFacts) -> str:
    if item.availability == OfferingAvailability.OUT_OF_STOCK:
        return "fully booked" if item.kind == OfferingKind.SERVICE else "sold out"
    return item.availability_text(with_quantity=True)


def _line(item: OfferingFacts, currency: str) -> str:
    return f"{item.ref} {item.name} - {item.price_text(currency)} - {_availability(item)}"


def shown_in_fallback(grounding: GroundingResult) -> list[OfferingFacts]:
    """The items the fallback reply lists (also what its cards show)."""
    if grounding.strict or grounding.mode == "unavailable":
        return []
    if grounding.mode == "detail":
        return [i for i in grounding.items if i.seq in grounding.matched_seqs] or grounding.items
    if grounding.mode == "browse":
        return grounding.items
    return []


def fallback_reply(grounding: GroundingResult) -> str:
    if grounding.strict or grounding.mode == "unavailable":
        return f"I can't check our list at the moment, so I won't quote a price. {ASK_TEAM}"
    shown = shown_in_fallback(grounding)
    if not shown:
        return f"I don't have that listed. {ASK_TEAM}"
    lines = [_line(i, grounding.currency) for i in shown]
    more = grounding.total_active - len(shown)
    if grounding.mode == "browse" and more > 0:
        lines.append(f"+{more} more items. Tell me what you are looking for.")
    return "Here is what we have listed:\n" + "\n".join(lines) + f"\n{ASK_TEAM}"
