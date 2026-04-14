"""Customer-facing authentication and profile models."""

import secrets
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString, hash_for_lookup
from app.db.base import Base, TimestampMixin, UUIDMixin


def generate_chat_user_key() -> str:
    """Generate a stable opaque chat identifier for customer conversations."""
    return secrets.token_urlsafe(24)


class CustomerUser(Base, UUIDMixin, TimestampMixin):
    """Logged-in customer account for the B2C experience."""

    __tablename__ = "customer_users"
    __table_args__ = (Index("ix_customer_users_email_hash_active", "email_hash", "is_active"),)

    full_name: Mapped[str] = mapped_column(
        EncryptedString,
        nullable=False,
        comment="Customer full name — encrypted at rest",
    )
    email: Mapped[str] = mapped_column(
        EncryptedString,
        nullable=False,
        comment="Customer email address — encrypted at rest",
    )
    email_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
        comment="SHA-256 hash of email for lookups",
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


@event.listens_for(CustomerUser, "before_insert")
@event.listens_for(CustomerUser, "before_update")
def _populate_customer_email_hash(mapper, connection, target):
    """Auto-populate email_hash from email before every insert/update.

    Callers never need to set email_hash manually — it's derived from email.
    """
    if target.email:
        target.email_hash = hash_for_lookup(target.email)
