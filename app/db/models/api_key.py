"""API Key model for widget authentication."""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Boolean, DateTime, Enum as SQLEnum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDMixin, TimestampMixin


class APIKeyType(str, enum.Enum):
    """API key type enum."""

    PUBLIC = "public"  # For widget (pk_live_...)
    SECRET = "secret"  # For server-to-server (sk_live_...)


class APIKey(Base, UUIDMixin, TimestampMixin):
    """
    API keys for widget and server authentication.

    Public keys (pk_live_...) are used in the widget embedded on customer websites.
    Secret keys (sk_live_...) are for server-to-server communication.
    """

    __tablename__ = "api_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant this API key belongs to",
    )

    key_type: Mapped[APIKeyType] = mapped_column(
        SQLEnum(APIKeyType, name="api_key_type"),
        nullable=False,
        index=True,
        comment="Type of API key (public or secret)",
    )

    key_prefix: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Key prefix for display (e.g., 'pk_live_abc...')",
    )

    key_hash: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
        comment="Hashed full API key",
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Descriptive name for this API key",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="Whether this API key is active",
    )

    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last time this API key was used",
    )

    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Expiration date (null = never expires)",
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        backref="api_keys",
    )

    def __repr__(self) -> str:
        return f"<APIKey(id={self.id}, key_prefix={self.key_prefix}, type={self.key_type}, active={self.is_active})>"
