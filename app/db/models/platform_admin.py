"""Platform admin models for Luran platform management."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDMixin, TimestampMixin


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
        String(255),
        unique=True,
        index=True,
        nullable=False,
        comment="Admin email address (unique across platform)",
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Bcrypt hashed password",
    )
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Full name of the admin",
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
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful login timestamp",
    )

    def __repr__(self) -> str:
        return f"<PlatformAdmin(id={self.id}, email={self.email}, role={self.role})>"
