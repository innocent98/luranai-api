"""User and authentication models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString, hash_for_lookup
from app.db.base import Base, TimestampMixin, UUIDMixin


class UserRole(str, enum.Enum):
    """User role enum for tenant users (not platform admins)."""

    TENANT_ADMIN = "tenant_admin"  # Admin access for specific tenant
    SUPPORT_AGENT = "support_agent"  # Can view and respond to tickets
    VIEWER = "viewer"  # Read-only access


class User(Base, UUIDMixin, TimestampMixin):
    """
    Tenant users (company staff members).

    Includes tenant admins, support agents, and viewers. All users must
    belong to a tenant. For platform administrators, see PlatformAdmin model.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_tenant_role", "tenant_id", "role"),
        Index("ix_users_email_hash_active", "email_hash", "is_active"),
    )

    email: Mapped[str] = mapped_column(
        EncryptedString,
        nullable=False,
        comment="User email address — encrypted at rest",
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
        comment="Full name of the user — encrypted at rest",
    )
    role: Mapped[UserRole] = mapped_column(
        SQLEnum(UserRole, name="user_role"),
        nullable=False,
        index=True,
        comment="User's role determining access level",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant this user belongs to (all users must belong to a tenant)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="Whether this user account is active",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful login timestamp",
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
        comment="Whether user must change password on next login (temp password)",
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email}, role={self.role})>"


@event.listens_for(User, "before_insert")
@event.listens_for(User, "before_update")
def _populate_user_email_hash(mapper, connection, target):
    """Auto-populate email_hash from email before every insert/update."""
    if target.email:
        target.email_hash = hash_for_lookup(target.email)
