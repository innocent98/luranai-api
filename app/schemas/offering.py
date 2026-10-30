"""Request bodies for the offerings API.

Business rules (price vs mode, lengths, ranges) are checked in the service so every violation
comes back in the module's flat `{code, message, errors[]}` body; the schemas only fix the types.
"""

from typing import Literal

from uuid import UUID

from pydantic import BaseModel

Kind = Literal["product", "service"]
PriceMode = Literal["fixed", "from", "on_request"]
Availability = Literal["in_stock", "limited", "out_of_stock", "on_request"]
Status = Literal["active", "archived"]


class OfferingCreate(BaseModel):
    kind: Kind
    name: str
    description: str | None = None
    category: str | None = None
    keywords: str | None = None
    price_mode: PriceMode
    price: int | None = None
    price_note: str | None = None
    availability: Availability
    quantity: int | None = None
    negotiable: bool = False


class OfferingPatch(BaseModel):
    """Any subset of the create fields, plus `status`. Absent = unchanged; `null` clears."""

    kind: Kind | None = None
    name: str | None = None
    description: str | None = None
    category: str | None = None
    keywords: str | None = None
    price_mode: PriceMode | None = None
    price: int | None = None
    price_note: str | None = None
    availability: Availability | None = None
    quantity: int | None = None
    negotiable: bool | None = None
    status: Status | None = None


class ImageOrder(BaseModel):
    """The photo ids in their new order (first = cover)."""

    image_ids: list[UUID]
