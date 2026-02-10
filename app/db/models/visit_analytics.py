"""Visit analytics models for daily traffic tracking."""

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDMixin


class DailyVisit(Base, UUIDMixin):
    """
    Aggregated daily visits keyed by visitor.

    One row per day/source/tenant/visitor, with an incrementing visit counter.
    """

    __tablename__ = "daily_visits"
    __table_args__ = (
        UniqueConstraint(
            "visit_date",
            "tenant_id",
            "source",
            "visitor_key",
            name="uq_daily_visits_per_visitor_day",
        ),
        Index("ix_daily_visits_date_source", "visit_date", "source"),
        Index("ix_daily_visits_tenant_date", "tenant_id", "visit_date"),
    )

    visit_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
        comment="UTC date bucket for the visit",
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Tenant associated with the visit",
    )
    source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Visit source (e.g., dashboard, chat_widget)",
    )
    visitor_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="Stable visitor identifier (hashed/anonymized where possible)",
    )
    visit_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Number of visits for this visitor on this day",
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="Timestamp for first visit event in this bucket",
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="Timestamp for most recent visit event in this bucket",
    )
    meta: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Optional metadata about the visit",
    )

    def __repr__(self) -> str:
        return (
            f"<DailyVisit(date={self.visit_date}, source={self.source}, "
            f"tenant_id={self.tenant_id}, visitor_key={self.visitor_key})>"
        )
