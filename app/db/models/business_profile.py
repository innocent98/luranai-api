"""BusinessProfile model — per-tenant synthesized 'what this business is/does'."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class BusinessProfileSource(str, enum.Enum):
    AUDITOR = "auditor"
    MANUAL = "manual"


class BusinessProfile(Base, UUIDMixin, TimestampMixin):
    """One synthesized business profile per tenant. Not PII -> not encrypted."""

    __tablename__ = "business_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    offerings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    audience: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    positioning: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_entities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tone: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[BusinessProfileSource] = mapped_column(
        SQLEnum(
            BusinessProfileSource,
            name="business_profile_source",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=BusinessProfileSource.AUDITOR,
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return f"<BusinessProfile(tenant_id={self.tenant_id}, industry={self.industry})>"
