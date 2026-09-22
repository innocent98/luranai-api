"""DeletionRequest — tracks an account-deletion lifecycle from request to purge.

A row is created whenever a user (or tenant admin, for a tenant-wide deletion)
requests deletion. It carries the request through email verification, the
cancellable grace period, and the eventual purge. ``User.deletion_status`` /
``User.deletion_requested_at`` mirror the *current* row's state onto the user
for cheap reads (e.g. blocking login); this table is the durable audit trail
and the source of truth for the worker that verifies, cancels, and purges.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.db.models.user import DeletionStatus


class DeletionScope(str, enum.Enum):
    """What the deletion request removes."""

    SELF_USER = "self_user"  # The requesting user's own account only
    TENANT = "tenant"  # The entire tenant (all users + tenant data)


class DeletionRequestedVia(str, enum.Enum):
    """Channel the deletion request originated from."""

    AUTHED = "authed"  # Initiated from within the authenticated dashboard
    PUBLIC = "public"  # Initiated from an unauthenticated public form


class DeletionRequest(Base, UUIDMixin, TimestampMixin):
    """One deletion lifecycle: requested -> verified -> grace -> purged (or cancelled)."""

    __tablename__ = "deletion_requests"
    __table_args__ = (
        Index("ix_deletion_requests_user_id", "user_id"),
        Index("ix_deletion_requests_tenant_id", "tenant_id"),
        Index("ix_deletion_requests_status_scheduled_purge", "status", "scheduled_purge_at"),
        Index("ix_deletion_requests_verification_token_hash", "verification_token_hash"),
        Index("ix_deletion_requests_cancel_token_hash", "cancel_token_hash"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="User who requested (or is the target of) this deletion",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this request belongs to (denormalised for tenant-scoped queries)",
    )
    scope: Mapped[DeletionScope] = mapped_column(
        SQLEnum(
            DeletionScope,
            name="deletion_scope",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        comment="Whether this deletes the single user or the whole tenant",
    )
    status: Mapped[DeletionStatus] = mapped_column(
        SQLEnum(
            DeletionStatus,
            name="deletion_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=DeletionStatus.PENDING_VERIFICATION,
        nullable=False,
        comment="Current stage of this deletion request's lifecycle",
    )
    verification_token_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hash of the pending email-verification token, if any",
    )
    cancel_token_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hash of the active grace-period cancel token, if any",
    )
    requested_via: Mapped[DeletionRequestedVia] = mapped_column(
        SQLEnum(
            DeletionRequestedVia,
            name="deletion_requested_via",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        comment="Whether this was requested from the authed dashboard or a public form",
    )
    scheduled_purge_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the grace period ends and the purge worker may act on this request",
    )

    def __repr__(self) -> str:
        return (
            f"<DeletionRequest(id={self.id}, user_id={self.user_id}, "
            f"scope={self.scope}, status={self.status})>"
        )
