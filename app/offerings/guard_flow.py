"""Run the guard on a reply: one regeneration of the same call type, then the code fallback."""

import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Iterable

from app.offerings.fallback import fallback_reply
from app.offerings.grounding import GroundingResult
from app.offerings.guard import GuardVerdict, check_reply

logger = logging.getLogger(__name__)

CORRECTIVE_NOTE = (
    "Correction: use only the prices in the offerings block, and never type an amount for a "
    "listed item. Write its price as its token, "
    "[[price:#N]], directly after that item's own name, one token per item, using only refs from "
    "the offerings block. No totals, quantities times a price, bundles, 'same price' claims, free "
    "extras or discounts. Put any delivery fee or other non-catalog amount in its own sentence, "
    "never in a sentence about a listed item. Never agree to, accept or confirm a price the "
    "customer proposed, never repeat their figure beside an item: say the owner is open to offers "
    "and the team will follow up."
)

# (reply text, sales_meta, token_count, is_api_error), as the model calls return it
Regenerate = Callable[[str], Awaitable[tuple[str, dict | None, int | None, bool]]]


@dataclass
class GuardOutcome:
    reply: str
    tokens: int | None
    sales_meta: dict | None
    record: dict
    offer_escalation: bool = False


async def guard_reply(
    reply: str,
    grounding: GroundingResult,
    *,
    regenerate: Regenerate,
    tokens: int | None,
    sales_meta: dict | None = None,
    customer_texts: Iterable[str] = (),
    kb_text: str = "",
    tier_amounts: Iterable[int] = (),
    offer_hint: bool = False,
) -> GuardOutcome:
    customer_texts = list(customer_texts)
    tier_amounts = list(tier_amounts)

    def verdict(text: str, meta: dict | None):
        offer = bool(((meta or {}).get("signals") or {}).get("offered_price"))
        try:
            return check_reply(
                text,
                grounding,
                customer_texts=customer_texts,
                kb_text=kb_text,
                tier_amounts=tier_amounts,
                offer_in_play=offer,
                offer_watch=offer_hint,
            )
        except Exception:  # noqa: BLE001 - a guard bug must end in the safe reply, not an apology
            logger.exception("offerings guard: check failed; treating the reply as rejected")
            return GuardVerdict(ok=False)

    record = {"checked": True, "blocked": False, "regenerated": False, "fell_back": False}
    if verdict(reply, sales_meta).ok:
        return GuardOutcome(reply, tokens, sales_meta, record)

    record["blocked"] = True
    record["regenerated"] = True
    total = tokens
    try:
        text, new_meta, new_tokens, api_error = await regenerate(CORRECTIVE_NOTE)
    except Exception:  # noqa: BLE001 - a failed regeneration must still end in a safe reply
        logger.exception("offerings guard: regeneration failed")
        text, new_meta, new_tokens, api_error = "", None, None, True
    if new_tokens:
        total = (total or 0) + new_tokens
    if not api_error:
        sales_meta = new_meta if new_meta is not None else sales_meta
        if verdict(text, sales_meta).ok:
            return GuardOutcome(text, total, sales_meta, record)

    record["fell_back"] = True
    logger.warning("offerings guard: falling back to the code reply")
    return GuardOutcome(
        fallback_reply(grounding), total, _without_pitch(sales_meta), record, offer_escalation=True
    )


def _without_pitch(sales_meta: dict | None) -> dict | None:
    """A rejected reply must not drive a quote or a meeting offer; lead capture and the cart stay."""
    if not sales_meta:
        return sales_meta
    signals = {**(sales_meta.get("signals") or {}), "offer_quote": False, "offer_meeting": False}
    return {**sales_meta, "signals": signals}
