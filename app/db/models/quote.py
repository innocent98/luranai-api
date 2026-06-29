"""Quote — an in-chat quotation computed from tenant pricing (Stage 3 Slice 4)."""

import enum
import uuid

from sqlalchemy import JSON, Enum as SQLEnum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class QuoteStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"


class Quote(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "quotes"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="NGN")
    line_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[QuoteStatus] = mapped_column(
        SQLEnum(QuoteStatus, name="quote_status", values_callable=lambda x: [e.value for e in x]),
        default=QuoteStatus.DRAFT, nullable=False)

    def __repr__(self) -> str:
        return f"<Quote(id={self.id}, tenant_id={self.tenant_id}, total={self.total})>"
