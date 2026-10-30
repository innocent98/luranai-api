"""A frozen snapshot of one catalog row, as the AI path sees it.

Everything the block, the price guard, `meta.offerings`, the cart and the code fallback reply say
about an item comes from one of these, built from a row read in the same turn. Money is whole
currency units; there is no float anywhere.
"""

import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingKind,
    OfferingPriceMode,
)
from app.sales.pricing import _currency_symbol

NAME_MAX = 120
DESCRIPTION_MAX = 240
_ANGLE = re.compile(r"[<>]")
_SPACE = re.compile(r"\s+")


def clean_text(value: str | None, limit: int) -> str:
    """Item text is data: no control characters, no angle brackets, one line, bounded."""
    if not value:
        return ""
    kept = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in value
    )
    kept = _SPACE.sub(" ", _ANGLE.sub("", kept)).strip()
    return kept if len(kept) <= limit else kept[: limit - 1].rstrip() + "…"


def format_amount(amount: int, currency: str = "NGN") -> str:
    return f"{_currency_symbol(currency)}{amount:,}"


_PRODUCT_AVAILABILITY = {
    OfferingAvailability.IN_STOCK: "in stock",
    OfferingAvailability.LIMITED: "limited stock",
    OfferingAvailability.OUT_OF_STOCK: "SOLD OUT — do not offer",
    OfferingAvailability.ON_REQUEST: "availability on request",
}
_SERVICE_AVAILABILITY = {
    OfferingAvailability.IN_STOCK: "available",
    OfferingAvailability.LIMITED: "limited slots",
    OfferingAvailability.OUT_OF_STOCK: "FULLY BOOKED — do not offer",
    OfferingAvailability.ON_REQUEST: "by request",
}


@dataclass(frozen=True)
class OfferingFacts:
    id: UUID
    seq: int
    name: str
    kind: OfferingKind
    category: str | None
    description: str
    price_mode: OfferingPriceMode
    price: int | None
    price_note: str | None
    availability: OfferingAvailability
    quantity: int | None
    negotiable: bool
    photo_count: int = 0
    cover_image_id: UUID | None = None
    cover_url: str | None = None

    @property
    def ref(self) -> str:
        return f"#{self.seq}"

    @property
    def sold_out(self) -> bool:
        return self.availability == OfferingAvailability.OUT_OF_STOCK

    @property
    def fixed_price(self) -> int | None:
        """The price a customer can be charged for: only a `fixed` item has one."""
        return self.price if self.price_mode == OfferingPriceMode.FIXED else None

    def price_text(self, currency: str = "NGN") -> str:
        if self.price_mode == OfferingPriceMode.ON_REQUEST or self.price is None:
            return "price on request"
        text = format_amount(self.price, currency)
        if self.price_mode == OfferingPriceMode.FROM:
            text = f"from {text}"
        return f"{text} {self.price_note}" if self.price_note else text

    def availability_text(self, *, with_quantity: bool = False) -> str:
        table = (
            _SERVICE_AVAILABILITY if self.kind == OfferingKind.SERVICE else _PRODUCT_AVAILABILITY
        )
        text = table[self.availability]
        if (
            with_quantity
            and self.availability == OfferingAvailability.LIMITED
            and self.quantity is not None
        ):
            text += f" (about {self.quantity} left)"
        return text

    def with_photos(
        self, count: int, cover_image_id: UUID | None, cover_url: str | None
    ) -> "OfferingFacts":
        from dataclasses import replace

        return replace(self, photo_count=count, cover_image_id=cover_image_id, cover_url=cover_url)


def facts_from_row(row: Offering) -> OfferingFacts:
    return OfferingFacts(
        id=row.id,
        seq=row.seq,
        name=clean_text(row.name, NAME_MAX),
        kind=row.kind,
        category=clean_text(row.category, 60) or None,
        description=clean_text(row.description, DESCRIPTION_MAX),
        price_mode=row.price_mode,
        price=row.price,
        price_note=clean_text(row.price_note, 60) or None,
        availability=row.availability,
        quantity=row.quantity,
        negotiable=bool(row.negotiable),
    )
