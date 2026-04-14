"""Platform admin models for Luran platform management."""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString, hash_for_lookup
from app.db.base import Base, TimestampMixin, UUIDMixin


class PlatformAdminRole(str, enum.Enum):
    """Platform admin role enum for access control."""

    SUPER_ADMIN = "SUPER_ADMIN"  # Full platform access
    ADMIN = "ADMIN"  # Can manage tenants and view data


class PlatformAdmin(Base, UUIDMixin, TimestampMixin):
    """
    Platform administrators who manage the Luran platform.

    These are Luran staff members, not tenant users. They have access
    to platform-wide features like tenant management, billing, and analytics.
    """

    __tablename__ = "platform_admins"

    email: Mapped[str] = mapped_column(
        EncryptedString,
        nullable=False,
        comment="Admin email address — encrypted at rest",
    )
    email_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=True,  # Nullable during migration; tightened post-backfill
        comment="SHA-256 hash of email for lookups",
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Bcrypt hashed password",
    )
    full_name: Mapped[str] = mapped_column(
        EncryptedString,
        nullable=False,
        comment="Full name of the admin — encrypted at rest",
    )
    role: Mapped[PlatformAdminRole] = mapped_column(
        SQLEnum(PlatformAdminRole, name="platform_admin_role", create_type=False),
        nullable=False,
        index=True,
        comment="Admin's role determining access level",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether this admin account is active",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful login timestamp",
    )

    def __repr__(self) -> str:
        return f"<PlatformAdmin(id={self.id}, email={self.email}, role={self.role})>"


@event.listens_for(PlatformAdmin, "before_insert")
@event.listens_for(PlatformAdmin, "before_update")
def _populate_platform_admin_email_hash(mapper, connection, target):
    """Auto-populate email_hash from email before every insert/update."""
    if target.email:
        target.email_hash = hash_for_lookup(target.email)
