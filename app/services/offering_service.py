"""Offerings catalog: item CRUD, the per-tenant `#seq` handle, plan cap and activity days.

Money is whole currency units. Every write first passes `require_feature` (a lapsed tenant gets
`403 feature_not_in_plan`; reads stay open) and, for creation and un-archiving, the plan cap is
checked while holding a row lock on the tenant's settings row, which is also what hands out
`seq`, so two concurrent creates can neither overshoot the cap nor collide on a number.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.offering import (
    MAX_PRICE,
    Offering,
    OfferingActivityDay,
    OfferingAvailability,
    OfferingImage,
    OfferingKind,
    OfferingPriceMode,
    OfferingStatus,
    TenantOfferingsSettings,
)
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.services.entitlements import Entitlements, require_feature
from app.services.offering_media import destroy_assets, gallery_url, thumb_url

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# Field limits (spec section 6).
_TEXT_LIMITS = {
    "name": 120,
    "description": 2000,
    "category": 60,
    "keywords": 200,
    "price_note": 60,
    "external_ref": 80,
}
# Bidirectional embeddings, overrides and isolates (U+202A-U+202E, U+2066-U+2069), by code point.
_BIDI = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}
_INVISIBLE = {chr(0x200B), chr(0xFEFF)}  # zero-width space, byte-order mark

FORMULA_STARTS = ("=", "+", "-", "@")  # a spreadsheet would run a name that starts with these
MAX_QUANTITY = 2_147_483_647  # the column is a 32-bit integer
_REQUIRED_ENUMS = ("kind", "price_mode", "availability")

_CURRENCY_SYMBOLS = {"NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€", "GHS": "GH₵", "KES": "KSh "}


class OfferingError(Exception):
    """Mapped to a flat `{code, message[, ...]}` JSON body by the handler registered in main.py."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def not_found() -> OfferingError:
    return OfferingError(404, "offering_not_found", "Offering not found")


def validation_error(errors: list[dict[str, str]]) -> OfferingError:
    return OfferingError(
        422,
        "validation",
        "Some fields are not valid.",
        errors=[{"field": e["field"], "code": e["code"]} for e in errors],
    )


# --- text and price helpers -----------------------------------------------------------------


def clean_text(value: str | None, *, multiline: bool = False) -> str | None:
    """Strip control and bidi-override characters (tenant text reaches the model prompt).

    Newlines and tabs survive only in `multiline` fields; elsewhere they become spaces. Empty
    results are None.
    """
    if value is None:
        return None
    out = []
    for ch in str(value):
        if ch in ("\n", "\t", "\r"):
            out.append("\n" if multiline and ch != "\t" else " ")
            continue
        if unicodedata.category(ch) == "Cc" or ch in _BIDI or ch in _INVISIBLE:
            continue
        out.append(ch)
    text = "".join(out).replace("\r", "").strip()
    return text or None


def format_price(
    mode: str | OfferingPriceMode,
    price: int | None,
    note: str | None = None,
    currency: str = "NGN",
) -> str:
    """The one wording of a price ("₦12,000", "From ₦5,000 per session", "Price on request").

    Shared by the item cards, the WhatsApp caption and the quote text so a figure is never
    formatted two different ways.
    """
    mode = mode.value if isinstance(mode, OfferingPriceMode) else mode
    if mode == "on_request" or price is None:
        return "Price on request"
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    text = f"{symbol}{price:,}"
    if mode == "from":
        text = f"From {text}"
    return f"{text} {note}" if note else text


def validate_candidate(raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Clean and validate a full item dict. Returns (clean values, [{field, code}])."""
    errors: list[dict[str, str]] = []
    clean: dict[str, Any] = dict(raw)

    for field, limit in _TEXT_LIMITS.items():
        if field not in raw:
            continue
        clean[field] = clean_text(raw[field], multiline=field == "description")
        if clean[field] is not None and len(clean[field]) > limit:
            errors.append({"field": field, "code": f"{field}_too_long"})
    if not clean.get("name"):
        errors.append({"field": "name", "code": "name_required"})
    elif clean["name"].startswith(FORMULA_STARTS):
        # Checked on the cleaned text: invisible and control characters must not hide the lead.
        errors.append({"field": "name", "code": "name_formula"})

    missing = {f for f in _REQUIRED_ENUMS if raw.get(f) is None}
    errors += [{"field": f, "code": f"{f}_required"} for f in _REQUIRED_ENUMS if f in missing]

    mode, price = raw.get("price_mode"), raw.get("price")
    if "price_mode" in missing:
        pass  # the price rules depend on the mode; the caller fixes that first
    elif mode == "on_request":
        if price is not None:
            errors.append({"field": "price", "code": "price_not_allowed"})
    elif price is None:
        errors.append({"field": "price", "code": "price_required"})
    elif not 0 < price <= MAX_PRICE:
        errors.append({"field": "price", "code": "price_out_of_range"})

    quantity = raw.get("quantity")
    if quantity is not None and not 0 <= quantity <= MAX_QUANTITY:
        errors.append({"field": "quantity", "code": "quantity_invalid"})
    return clean, errors


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def image_to_dict(image: OfferingImage) -> dict[str, Any]:
    return {
        "id": str(image.id),
        "url": image.url,
        "thumb_url": thumb_url(image.url),
        "gallery_url": gallery_url(image.url),
        "position": image.position,
        "width": image.width,
        "height": image.height,
        "bytes": image.bytes,
    }


def offering_to_dict(item: Offering, images: list[OfferingImage] | None = None) -> dict[str, Any]:
    photos = [image_to_dict(i) for i in sorted(images or [], key=lambda i: i.position)]
    return {
        "id": str(item.id),
        "seq": item.seq,
        "ref": f"#{item.seq}",
        "kind": item.kind.value,
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "keywords": item.keywords,
        "price_mode": item.price_mode.value,
        "price": item.price,
        "price_note": item.price_note,
        "availability": item.availability.value,
        "quantity": item.quantity,
        "negotiable": item.negotiable,
        "status": item.status.value,
        "external_ref": item.external_ref,
        "images": photos,
        "photo_count": len(photos),
        "cover_url": photos[0]["thumb_url"] if photos else None,
        "availability_changed_at": _iso(item.availability_changed_at),
        "archived_at": _iso(item.archived_at),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class OfferingService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # -- plumbing ------------------------------------------------------------------------

    def require_write(self, tenant_id: UUID) -> Entitlements:
        """403 `feature_not_in_plan` for a lapsed tenant (or a plan with the feature at 0)."""
        return require_feature(self.db, tenant_id, "offerings")

    def default_currency(self, tenant_id: UUID) -> str:
        currency = self.db.execute(
            select(TenantSalesAgentConfig.currency).where(
                TenantSalesAgentConfig.tenant_id == tenant_id
            )
        ).scalar_one_or_none()
        return (currency or "NGN").upper()[:3]

    def lock_settings(self, tenant_id: UUID) -> TenantOfferingsSettings:
        """The tenant's settings row, created on first use and locked FOR UPDATE."""
        self.db.execute(
            pg_insert(TenantOfferingsSettings)
            .values(id=uuid4(), tenant_id=tenant_id, currency=self.default_currency(tenant_id))
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        return self.db.execute(
            select(TenantOfferingsSettings)
            .where(TenantOfferingsSettings.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

    def active_count(self, tenant_id: UUID) -> int:
        return int(
            self.db.execute(
                select(func.count())
                .select_from(Offering)
                .where(Offering.tenant_id == tenant_id, Offering.status == OfferingStatus.ACTIVE)
            ).scalar_one()
        )

    def _check_cap(self, tenant_id: UUID, entitlements: Entitlements, extra: int = 1) -> None:
        limit = entitlements.limit("offerings")
        if limit is None:
            return
        used = self.active_count(tenant_id)
        if used + extra > limit:
            raise OfferingError(
                409,
                "offering_limit",
                f"Your plan allows {limit:,} items. Archive or delete one, or upgrade, "
                "to add more.",
                limit=limit,
                used=used,
            )

    def record_activity(self, tenant_id: UUID) -> None:
        """Mark today (UTC) as a day the owner touched the catalog."""
        self.db.execute(
            pg_insert(OfferingActivityDay)
            .values(tenant_id=tenant_id, day=_now().date())
            .on_conflict_do_nothing()
        )

    def _images_by_item(self, item_ids: list[UUID]) -> dict[UUID, list[OfferingImage]]:
        if not item_ids:
            return {}
        rows = self.db.execute(
            select(OfferingImage)
            .where(OfferingImage.offering_id.in_(item_ids))
            .order_by(OfferingImage.position)
        ).scalars()
        grouped: dict[UUID, list[OfferingImage]] = {}
        for row in rows:
            grouped.setdefault(row.offering_id, []).append(row)
        return grouped

    def serialize(self, item: Offering) -> dict[str, Any]:
        return offering_to_dict(item, self._images_by_item([item.id]).get(item.id, []))

    def get_row(self, tenant_id: UUID, offering_id: UUID) -> Offering:
        item = self.db.execute(
            select(Offering).where(Offering.id == offering_id, Offering.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if item is None:
            raise not_found()
        return item

    def get_row_locked(self, tenant_id: UUID, offering_id: UUID) -> Offering:
        """The item row locked FOR UPDATE (photo uploads and reorders serialise on it)."""
        item = self.db.execute(
            select(Offering)
            .where(Offering.id == offering_id, Offering.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if item is None:
            raise not_found()
        return item

    # -- reads ---------------------------------------------------------------------------

    def get(self, tenant_id: UUID, offering_id: UUID) -> dict[str, Any]:
        return self.serialize(self.get_row(tenant_id, offering_id))

    def list_items(
        self,
        tenant_id: UUID,
        entitlements: Entitlements,
        *,
        status: str = "active",
        kind: str | None = None,
        category: str | None = None,
        availability: str | None = None,
        q: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conditions = [Offering.tenant_id == tenant_id, Offering.status == OfferingStatus(status)]
        if kind:
            conditions.append(Offering.kind == OfferingKind(kind))
        if availability:
            conditions.append(Offering.availability == OfferingAvailability(availability))
        if category:
            conditions.append(func.lower(Offering.category) == category.strip().lower())
        if q and q.strip():
            like = f"%{_escape_like(q.strip())}%"
            conditions.append(
                or_(
                    Offering.name.ilike(like, escape="\\"),
                    Offering.keywords.ilike(like, escape="\\"),
                    Offering.category.ilike(like, escape="\\"),
                )
            )
        total = int(
            self.db.execute(
                select(func.count()).select_from(Offering).where(*conditions)
            ).scalar_one()
        )
        page_conditions = list(conditions)
        if cursor is not None:
            try:
                page_conditions.append(Offering.seq < int(cursor))
            except ValueError:
                raise OfferingError(422, "invalid_cursor", "The cursor is not valid.") from None
        rows = list(
            self.db.execute(
                select(Offering)
                .where(*page_conditions)
                .order_by(Offering.seq.desc())
                .limit(limit + 1)
            ).scalars()
        )
        next_cursor = str(rows[limit - 1].seq) if len(rows) > limit else None
        rows = rows[:limit]
        images = self._images_by_item([r.id for r in rows])
        data = [offering_to_dict(r, images.get(r.id, [])) for r in rows]
        meta = {
            "next_cursor": next_cursor,
            "total": total,
            "page_size": limit,
            "limit": entitlements.limit("offerings"),
            "used": self.active_count(tenant_id),
        }
        return data, meta

    # -- writes --------------------------------------------------------------------------

    def create(self, tenant_id: UUID, fields: dict[str, Any]) -> dict[str, Any]:
        entitlements = self.require_write(tenant_id)
        clean, errors = validate_candidate(fields)
        if errors:
            raise validation_error(errors)
        settings = self.lock_settings(tenant_id)
        self._check_cap(tenant_id, entitlements)
        now = _now()
        item = Offering(
            tenant_id=tenant_id,
            seq=settings.next_seq,
            kind=OfferingKind(clean["kind"]),
            name=clean["name"],
            description=clean.get("description"),
            category=clean.get("category"),
            keywords=clean.get("keywords"),
            price_mode=OfferingPriceMode(clean["price_mode"]),
            price=clean.get("price"),
            price_note=clean.get("price_note"),
            availability=OfferingAvailability(clean["availability"]),
            quantity=clean.get("quantity"),
            negotiable=bool(clean.get("negotiable", False)),
            status=OfferingStatus.ACTIVE,
            external_ref=clean.get("external_ref"),
            availability_changed_at=now,
            created_at=now,
            updated_at=now,
        )
        settings.next_seq += 1
        self.db.add(item)
        self.db.flush()
        self.record_activity(tenant_id)
        self.db.commit()
        self.db.refresh(item)
        return offering_to_dict(item, [])

    def patch(self, tenant_id: UUID, offering_id: UUID, changes: dict[str, Any]) -> dict[str, Any]:
        """Partial update. `changes` holds only the fields the caller sent (explicit None clears)."""
        entitlements = self.require_write(tenant_id)
        item = self.get_row(tenant_id, offering_id)

        current = {
            "kind": item.kind.value,
            "name": item.name,
            "description": item.description,
            "category": item.category,
            "keywords": item.keywords,
            "price_mode": item.price_mode.value,
            "price": item.price,
            "price_note": item.price_note,
            "availability": item.availability.value,
            "quantity": item.quantity,
            "negotiable": item.negotiable,
        }
        status = changes.pop("status", None)
        candidate = {**current, **changes}
        # Switching to "on request" without a price sheds the old one; the reverse needs one.
        if candidate["price_mode"] == "on_request" and "price" not in changes:
            candidate["price"] = None
        if candidate["negotiable"] is None:
            candidate["negotiable"] = False
        clean, errors = validate_candidate(candidate)
        if errors:
            raise validation_error(errors)

        now = _now()
        if status == "active" and item.status != OfferingStatus.ACTIVE:
            self.lock_settings(tenant_id)
            self._check_cap(tenant_id, entitlements)
            item.status = OfferingStatus.ACTIVE
            item.archived_at = None
        elif status == "archived" and item.status != OfferingStatus.ARCHIVED:
            item.status = OfferingStatus.ARCHIVED
            item.archived_at = now

        if clean["availability"] != current["availability"]:
            item.availability_changed_at = now
        item.kind = OfferingKind(clean["kind"])
        item.name = clean["name"]
        item.description = clean.get("description")
        item.category = clean.get("category")
        item.keywords = clean.get("keywords")
        item.price_mode = OfferingPriceMode(clean["price_mode"])
        item.price = clean.get("price")
        item.price_note = clean.get("price_note")
        item.availability = OfferingAvailability(clean["availability"])
        item.quantity = clean.get("quantity")
        item.negotiable = bool(clean["negotiable"])
        item.updated_at = now
        self.db.flush()
        self.record_activity(tenant_id)
        self.db.commit()
        self.db.refresh(item)
        return self.serialize(item)

    def archive(self, tenant_id: UUID, offering_id: UUID) -> None:
        self.require_write(tenant_id)
        item = self.get_row(tenant_id, offering_id)
        if item.status != OfferingStatus.ARCHIVED:
            now = _now()
            item.status = OfferingStatus.ARCHIVED
            item.archived_at = now
            item.updated_at = now
        self.record_activity(tenant_id)
        self.db.commit()

    def hard_delete(self, tenant_id: UUID, offering_id: UUID) -> None:
        """Delete the row (photos cascade), then destroy the photo assets (best-effort)."""
        self.require_write(tenant_id)
        item = self.get_row(tenant_id, offering_id)
        public_ids = list(
            self.db.execute(
                select(OfferingImage.cloudinary_public_id).where(
                    OfferingImage.offering_id == item.id
                )
            ).scalars()
        )
        self.db.delete(item)
        self.record_activity(tenant_id)
        self.db.commit()
        destroy_assets(public_ids)
