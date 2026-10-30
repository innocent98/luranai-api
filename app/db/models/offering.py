"""Offerings catalog: the products and services a tenant sells, read live by the AI on every channel.

Money is whole currency units (naira), the same unit as `Quote.total` and the sales agent's
`pricing_tiers`; there is deliberately one unit. Enum-typed columns are stored as short strings
guarded by named CHECK constraints (`native_enum=False`), so adding a value later is a constraint
swap rather than a Postgres `ALTER TYPE`.
"""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin

MAX_PRICE = 10_000_000_000  # whole naira; a sanity ceiling, not a business rule


class OfferingKind(str, enum.Enum):
    PRODUCT = "product"
    SERVICE = "service"


class OfferingPriceMode(str, enum.Enum):
    FIXED = "fixed"
    FROM = "from"
    ON_REQUEST = "on_request"


class OfferingAvailability(str, enum.Enum):
    IN_STOCK = "in_stock"
    LIMITED = "limited"
    OUT_OF_STOCK = "out_of_stock"
    ON_REQUEST = "on_request"


class OfferingStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


def _str_enum(enum_cls: type[enum.Enum], name: str) -> SQLEnum:
    """VARCHAR + a named CHECK constraint holding the enum's values."""
    return SQLEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda x: [e.value for e in x],
    )


class Offering(Base, UUIDMixin, TimestampMixin):
    """One product or service. `seq` is the per-tenant, never-reused `#12` handle."""

    __tablename__ = "offerings"
    __table_args__ = (
        Index("ix_offerings_tenant_status", "tenant_id", "status"),
        # NULL external_refs never collide in Postgres, so this is "unique where set".
        UniqueConstraint("tenant_id", "seq", name="uq_offerings_tenant_seq"),
        UniqueConstraint("tenant_id", "external_ref", name="uq_offerings_tenant_external_ref"),
        CheckConstraint(
            "(price_mode = 'on_request' AND price IS NULL) OR "
            f"(price_mode <> 'on_request' AND price IS NOT NULL AND price > 0 AND price <= {MAX_PRICE})",
            name="ck_offerings_price_matches_mode",
        ),
        CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_offerings_quantity"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[OfferingKind] = mapped_column(_str_enum(OfferingKind, "ck_offerings_kind"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    keywords: Mapped[str | None] = mapped_column(String(200), nullable=True)
    price_mode: Mapped[OfferingPriceMode] = mapped_column(
        _str_enum(OfferingPriceMode, "ck_offerings_price_mode")
    )
    price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price_note: Mapped[str | None] = mapped_column(String(60), nullable=True)
    availability: Mapped[OfferingAvailability] = mapped_column(
        _str_enum(OfferingAvailability, "ck_offerings_availability")
    )
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    negotiable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[OfferingStatus] = mapped_column(
        _str_enum(OfferingStatus, "ck_offerings_status"),
        nullable=False,
        default=OfferingStatus.ACTIVE,
        server_default=OfferingStatus.ACTIVE.value,
    )
    external_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    availability_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OfferingImage(Base, UUIDMixin):
    """A photo of an item. `position` 0 is the cover. Only the optimised copy is stored."""

    __tablename__ = "offering_images"
    __table_args__ = (
        # Not DEFERRABLE: the suite builds every table on SQLite too, which cannot declare it.
        # A reorder or a delete therefore moves positions in two steps (see the image service).
        UniqueConstraint("offering_id", "position", name="uq_offering_images_position"),
    )

    offering_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("offerings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    cloudinary_public_id: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OfferingActivityDay(Base):
    """A day on which the owner created, edited or confirmed (source of the daily-upkeep measure)."""

    __tablename__ = "offering_activity_days"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)


class TenantOfferingsSettings(Base, UUIDMixin, TimestampMixin):
    """One row per tenant, created lazily. `next_seq` is the allocator behind `#12`."""

    __tablename__ = "tenant_offerings_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="NGN", server_default="NGN"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale_after_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class LeadOfferingInterest(Base, UUIDMixin):
    """An item a customer chose, with snapshots so a later edit never rewrites what they saw."""

    __tablename__ = "lead_offering_interests"
    __table_args__ = (CheckConstraint("qty >= 1", name="ck_lead_offering_interests_qty"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    offering_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("offerings.id", ondelete="SET NULL"), nullable=True
    )
    ref: Mapped[str] = mapped_column(String(20), nullable=False)
    name_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    price_mode_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    price_snapshot: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price_note_snapshot: Mapped[str | None] = mapped_column(String(60), nullable=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    offered_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OfferingMiss(Base, UUIDMixin):
    """What customers asked for that is not listed. Aggregated: no session, customer or raw text."""

    __tablename__ = "offering_misses"
    __table_args__ = (UniqueConstraint("tenant_id", "term", name="uq_offering_misses_tenant_term"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    term: Mapped[str] = mapped_column(String(80), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
