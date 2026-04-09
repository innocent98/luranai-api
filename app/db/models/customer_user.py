"""Customer-facing authentication and profile models."""

import secrets
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


def generate_chat_user_key() -> str:
    """Generate a stable opaque chat identifier for customer conversations."""
    return secrets.token_urlsafe(24)


class CustomerUser(Base, UUIDMixin, TimestampMixin):
    """Logged-in customer account for the B2C experience."""

    __tablename__ = "customer_users"
    __table_args__ = (Index("ix_customer_users_email_active", "email", "is_active"),)

    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Customer full name",
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
        comment="Customer email address",
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Bcrypt hashed password",
    )
    chat_user_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
        default=generate_chat_user_key,
        comment="Opaque stable identifier used for customer chat transport",
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the customer verified their email",
    )
    avatar_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Optional customer avatar",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
        comment="Whether the customer account is active",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful login timestamp",
    )

    def __repr__(self) -> str:
        return f"<CustomerUser(id={self.id}, email={self.email})>"
