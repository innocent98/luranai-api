"""Payment transaction database models."""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PaymentTransaction(Base):
    """Payment transaction records."""

    __tablename__ = "payment_transactions"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        index=True,
    )

    # Tenant relationship
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Transaction details
    reference: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="Paystack transaction reference",
    )
    amount: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Amount in kobo (₦1 = 100 kobo)",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        default="NGN",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        nullable=False,
        index=True,
        comment="Transaction status: pending, success, failed, abandoned",
    )

    # Subscription details
    tier: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Subscription tier: lite, professional, enterprise",
    )
    billing_cycle: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Billing cycle: monthly, annual",
    )

    # Customer details
    customer_email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    customer_code: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="Paystack customer code",
    )

    # Payment details
    channel: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Payment channel: card, bank, ussd, etc.",
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When payment was completed",
    )

    # Subscription tracking
    subscription_code: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
        comment="Paystack subscription code (for recurring)",
    )
    authorization_code: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="Paystack authorization code (for recurring)",
    )

    # Paystack raw data
    paystack_data: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        comment="Raw Paystack response data",
    )

    # Error tracking
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if transaction failed",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(datetime.now().astimezone().tzinfo),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(datetime.now().astimezone().tzinfo),
        onupdate=lambda: datetime.now(datetime.now().astimezone().tzinfo),
        nullable=False,
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="payment_transactions")


# Add to Tenant model's relationships
# This should be added to app/db/models/tenant.py:
# payment_transactions: Mapped[list["PaymentTransaction"]] = relationship(
#     "PaymentTransaction", back_populates="tenant", cascade="all, delete-orphan"
# )
