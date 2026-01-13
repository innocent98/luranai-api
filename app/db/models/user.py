"""User and authentication models."""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Enum as SQLEnum, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import ForeignKey

from app.db.base import Base, UUIDMixin, TimestampMixin


class UserRole(str, enum.Enum):
    """User role enum for access control."""

    SUPER_ADMIN = "super_admin"  # Full platform access across all tenants
    TENANT_ADMIN = "tenant_admin"  # Admin access for specific tenant
    SUPPORT_AGENT = "support_agent"  # Can view and respond to tickets
    VIEWER = "viewer"  # Read-only access


class User(Base, UUIDMixin, TimestampMixin):
    """
    Admin users and support agents.

    Supports both platform-wide super admins and tenant-specific users
    with role-based access control.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_tenant_role", "tenant_id", "role"),
        Index("ix_users_email_active", "email", "is_active"),
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
        comment="User email address (unique across platform)",
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Bcrypt hashed password",
    )
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Full name of the user",
    )
    role: Mapped[UserRole] = mapped_column(
        SQLEnum(UserRole, name="user_role"),
        nullable=False,
        index=True,
        comment="User's role determining access level",
    )
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Tenant this user belongs to (null for super_admin)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="Whether this user account is active",
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether this user has superuser privileges",
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful login timestamp",
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email}, role={self.role})>"
