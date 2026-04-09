"""add customer follows and tenant channels

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-04-03 18:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c6d7e8f9a0b1"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_business_follows",
        sa.Column("customer_user_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_viewed_channel_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["customer_user_id"], ["customer_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_user_id",
            "tenant_id",
            name="uq_customer_business_follows_customer_tenant",
        ),
    )
    op.create_index(
        "ix_customer_business_follows_customer_created",
        "customer_business_follows",
        ["customer_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_customer_business_follows_tenant_created",
        "customer_business_follows",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_customer_business_follows_customer_user_id"),
        "customer_business_follows",
        ["customer_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_customer_business_follows_tenant_id"),
        "customer_business_follows",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_customer_business_follows_id"),
        "customer_business_follows",
        ["id"],
        unique=False,
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_type
                WHERE typname = 'channel_post_status'
            ) THEN
                CREATE TYPE channel_post_status AS ENUM ('draft', 'published', 'archived');
            END IF;
        END
        $$;
        """
    )

    channel_post_status = postgresql.ENUM(
        "draft",
        "published",
        "archived",
        name="channel_post_status",
        create_type=False,
    )

    op.create_table(
        "tenant_channel_posts",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("published_by_user_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("excerpt", sa.String(length=320), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("cover_image_url", sa.String(length=500), nullable=True),
        sa.Column("cta_label", sa.String(length=80), nullable=True),
        sa.Column("cta_url", sa.String(length=1000), nullable=True),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", channel_post_status, nullable=False, server_default="draft"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tenant_channel_posts_tenant_status",
        "tenant_channel_posts",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_channel_posts_published_at",
        "tenant_channel_posts",
        ["tenant_id", "published_at"],
        unique=False,
    )
    op.create_index(op.f("ix_tenant_channel_posts_id"), "tenant_channel_posts", ["id"], unique=False)
    op.create_index(op.f("ix_tenant_channel_posts_status"), "tenant_channel_posts", ["status"], unique=False)
    op.create_index(op.f("ix_tenant_channel_posts_tenant_id"), "tenant_channel_posts", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_channel_posts_tenant_id"), table_name="tenant_channel_posts")
    op.drop_index(op.f("ix_tenant_channel_posts_status"), table_name="tenant_channel_posts")
    op.drop_index(op.f("ix_tenant_channel_posts_id"), table_name="tenant_channel_posts")
    op.drop_index("ix_tenant_channel_posts_published_at", table_name="tenant_channel_posts")
    op.drop_index("ix_tenant_channel_posts_tenant_status", table_name="tenant_channel_posts")
    op.drop_table("tenant_channel_posts")

    op.drop_index(op.f("ix_customer_business_follows_id"), table_name="customer_business_follows")
    op.drop_index(op.f("ix_customer_business_follows_tenant_id"), table_name="customer_business_follows")
    op.drop_index(op.f("ix_customer_business_follows_customer_user_id"), table_name="customer_business_follows")
    op.drop_index("ix_customer_business_follows_tenant_created", table_name="customer_business_follows")
    op.drop_index("ix_customer_business_follows_customer_created", table_name="customer_business_follows")
    op.drop_table("customer_business_follows")

    channel_post_status = postgresql.ENUM(
        "draft",
        "published",
        "archived",
        name="channel_post_status",
        create_type=False,
    )
    channel_post_status.drop(op.get_bind(), checkfirst=True)
