"""Customer follows and tenant channel post models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum as SQLEnum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class ChannelPostStatus(str, enum.Enum):
    """Tenant channel post lifecycle."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class CustomerBusinessFollow(Base, UUIDMixin, TimestampMixin):
    """Businesses a customer chose to follow for updates."""

    __tablename__ = "customer_business_follows"
    __table_args__ = (
        UniqueConstraint(
            "customer_user_id",
            "tenant_id",
            name="uq_customer_business_follows_customer_tenant",
        ),
        Index(
            "ix_customer_business_follows_customer_created",
            "customer_user_id",
            "created_at",
        ),
        Index(
            "ix_customer_business_follows_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    customer_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the customer wants update notifications from this business",
    )
    last_viewed_channel_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the customer last viewed channel updates for this business",
    )


class TenantChannelPost(Base, UUIDMixin, TimestampMixin):
    """Business-authored customer channel posts."""

    __tablename__ = "tenant_channel_posts"
    __table_args__ = (
        Index("ix_tenant_channel_posts_tenant_status", "tenant_id", "status"),
        Index(
            "ix_tenant_channel_posts_published_at",
            "tenant_id",
            "published_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    excerpt: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        comment="Short preview shown in feeds and business profile highlights",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Full rich-text/plain-text business update",
    )
    cover_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cta_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cta_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_pinned: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Pinned items surface first in feeds",
    )
    status: Mapped[ChannelPostStatus] = mapped_column(
        SQLEnum(ChannelPostStatus, name="channel_post_status"),
        default=ChannelPostStatus.DRAFT,
        nullable=False,
        index=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
