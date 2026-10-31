"""Bargaining turns are answered by code, never by the model (decision D1, review S-B).

A turn is an offer turn when the customer's latest message names a figure below the price of the
item under discussion, asks for a percentage, a discount or "the last price", or the model returned
an offered price. The model's text is dropped and the reply is built from the rows: the price is
firm, or the owner is open to offers, or a human takes over. This removes the free text in which an
agreement could hide, whatever words it used.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from app.offerings.amounts import customer_amounts, normalise
from app.offerings.cart import CartResolution
from app.offerings.facts import OfferingFacts, format_amount
from app.offerings.grounding import GroundingResult

ASK_TEAM = "Would you like me to connect you with the team?"
HANDOVER_LINE = "I'm handing this to the team now; they'll take it from here."

# STRONG bargaining wording: it asks for a lower price whether or not a figure is named. Wording that
# is only a bargain beside a figure ("can you do", "make it", "will you take", "how about", "I'll
# pay", "abeg") is not here: the figure itself (`customer_amounts`) makes those offers, so "can you
# do delivery to Lekki?" and "how about the red one?" stay ordinary questions.
_BARGAIN = re.compile(
    r"\b(?:last\s+price|final\s+price|lowest\s+price|better\s+price|special\s+price"
    r"|(?:your|ur)\s+best\s+price|what(?:'s|\s+is)\s+the\s+best\s+price|give\s+me\s+(?:a\s+)?best\s+price"
    r"|reduc(?:e|ed|tion|tions)(?:\s+am|\s+it|\s+the|\s+your)?|discount(?:s|ed)?|(?:make|do|give)\s+(?:it|me)\s+(?:a\s+bit\s+|little\s+)?cheaper|cheaper\s+price|half[\s-]?price"
    r"|bargain(?:ing)?|knock(?:\s+\w+)?\s+off|lower\s+(?:the\s+)?price|price\s+(?:drop|down|cut)"
    r"|drop\s+(?:the\s+)?price|(?:the\s+)?price\s+(?:is\s+)?(?:too\s+)?(?:high|much|steep)"
    r"|too\s+(?:high|expensive|costly|steep|cost)|too\s+much\s+(?:for\s+me|money)|e\s+too\s+much"
    r"|e\s+(?:don\s+|dey\s+)?too\s+cost|abeg\s+(?:last|small|do\s+something|make)|dash\s+me|last\s+last"
    r"|any\s+(?:small\s+)?offers?|pay\s+half|haggl\w*|"
    r"din\s+owo|won\s+ju|owo\s+re\s+ga\s+ju|rage(?:\s+\w+)?\s+(?:kudi|farashi\w*|mani)|rage\s+\w+"
    r"|ya\s+yi\s+tsada|tsada|belata)\b"
    r"|\b\d[\d.]*\s*(?:%|percent|per\s+cent)\s*(?:off|discount|less|reduction|cheaper)"
    r"|\b(?:give|take|knock)\s+(?:me\s+)?\d[\d.]*\s*(?:%|percent|per\s+cent)",
    re.IGNORECASE,
)


def _fold(text: str) -> str:
    """Accents folded away ("ẹ jọ ẹ din owo rẹ" reads "e jo e din owo re"), width folded too."""
    decomposed = unicodedata.normalize("NFKD", normalise(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).replace("ọ", "o")


def _matched(grounding: GroundingResult) -> list[OfferingFacts]:
    return [i for i in grounding.items if i.seq in grounding.matched_seqs]


def is_offer_turn(message: str, grounding: GroundingResult, offered_signal: bool) -> bool:
    """Strong signals only: bargaining wording, or a money figure below the price in play. An
    ordinary question that merely contains a number or a bargaining-looking verb is not one."""
    if grounding.strict or grounding.mode == "unavailable":
        return False
    if offered_signal or _BARGAIN.search(_fold(message)):
        return True
    prices = [i.price for i in _matched(grounding) if i.price is not None]
    if not prices:
        return False
    low = min(prices)
    return any(a.value < low for a in customer_amounts(message, small_ok=low < 1000))


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
