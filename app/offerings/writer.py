"""Which items a reply is about, and the `meta.offerings.items` cards (frozen contract, spec 11).

Cards are built from rows read this turn (the injected facts, plus photos re-read here), never from
model text: the name, price and availability on a card cannot differ from the database.
"""

import re

from app.offerings.fallback import shown_in_fallback
from app.offerings.facts import OfferingFacts
from app.offerings.grounding import GroundingResult
from app.offerings.signals import MAX_REFS, parse_catalog_signals
from app.services.offering_media import thumb_url


def _mentions(item: OfferingFacts, reply: str) -> int | None:
    """Position of the first mention (`#12` or the full name) in the reply, else None."""
    lowered = reply.lower()
    positions = []
    ref = re.search(rf"#\s?{item.seq}\b", reply)
    if ref:
        positions.append(ref.start())
    if item.name:
        at = lowered.find(item.name.lower())
        if at >= 0:
            positions.append(at)
    return min(positions) if positions else None


def shown_items(
    grounding: GroundingResult,
    reply: str,
    *,
    sales_signals: dict | None,
    fell_back: bool = False,
) -> list[OfferingFacts]:
    if not grounding.items:
        return []
    if fell_back:
        return shown_in_fallback(grounding)[:MAX_REFS]
    by_seq = {i.seq: i for i in grounding.items}
    chosen: list[OfferingFacts] = []
    if sales_signals is not None:
        chosen = [
            by_seq[s] for s in parse_catalog_signals(sales_signals).catalog_seqs if s in by_seq
        ]
    if not chosen:
        found = [(pos, i) for i in grounding.items if (pos := _mentions(i, reply)) is not None]
        chosen = [i for _, i in sorted(found, key=lambda p: p[0])]
    return chosen[:MAX_REFS]


def card(item: OfferingFacts, currency: str) -> dict:
    return {
        "ref": item.ref,
        "offering_id": str(item.id),
        "name": item.name,
        "price_text": item.price_text(currency),
        "availability": item.availability.value,
        "cover_image_id": str(item.cover_image_id) if item.cover_image_id else None,
        "cover_url": thumb_url(item.cover_url) if item.cover_url else None,
        "photo_count": item.photo_count,
    }
