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


class ImportRequest(BaseModel):
    """Preview and apply share this body (`skip_errors` only matters to apply)."""

    format: Literal["csv", "lines"]
    content: str
    mode: Literal["add", "upsert"] = "add"
    skip_errors: bool = False


class SettingsPatch(BaseModel):
    """Any subset; an omitted or null field is left as it is. Ranges are checked in the service."""

    enabled: bool | None = None
    stale_after_days: int | None = None
    currency: str | None = None
