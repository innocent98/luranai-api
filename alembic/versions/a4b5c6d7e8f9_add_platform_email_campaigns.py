"""add platform email campaigns

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-04-02 10:15:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "a4b5c6d7e8f9"
down_revision = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_email_campaigns",
        sa.Column("created_by_admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("audience_type", sa.String(length=50), nullable=False),
        sa.Column("template_key", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="queued", nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("preheader", sa.String(length=255), nullable=True),
        sa.Column("heading", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("cta_label", sa.String(length=120), nullable=True),
        sa.Column("cta_url", sa.String(length=1000), nullable=True),
        sa.Column("audience_summary", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("total_recipients", sa.Integer(), server_default="0", nullable=False),
        sa.Column("success_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_admin_id"], ["platform_admins.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_email_campaigns_admin_created",
        "platform_email_campaigns",
        ["created_by_admin_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaigns_created_by_admin_id"),
        "platform_email_campaigns",
        ["created_by_admin_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaigns_id"),
        "platform_email_campaigns",
        ["id"],
        unique=False,
    )
    op.create_index(
        "ix_platform_email_campaigns_status_created",
        "platform_email_campaigns",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaigns_status"),
        "platform_email_campaigns",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaigns_audience_type"),
        "platform_email_campaigns",
        ["audience_type"],
        unique=False,
    )

    op.create_table(
        "platform_email_campaign_recipients",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recipient_email", sa.String(length=255), nullable=False),
        sa.Column("recipient_name", sa.String(length=255), nullable=True),
        sa.Column("tenant_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["platform_email_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_email_campaign_recipients_campaign_status",
        "platform_email_campaign_recipients",
        ["campaign_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_platform_email_campaign_recipients_email_campaign",
        "platform_email_campaign_recipients",
        ["recipient_email", "campaign_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaign_recipients_campaign_id"),
        "platform_email_campaign_recipients",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaign_recipients_id"),
        "platform_email_campaign_recipients",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaign_recipients_status"),
        "platform_email_campaign_recipients",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaign_recipients_tenant_id"),
        "platform_email_campaign_recipients",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_email_campaign_recipients_user_id"),
        "platform_email_campaign_recipients",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_platform_email_campaign_recipients_user_id"), table_name="platform_email_campaign_recipients")
    op.drop_index(op.f("ix_platform_email_campaign_recipients_tenant_id"), table_name="platform_email_campaign_recipients")
    op.drop_index(op.f("ix_platform_email_campaign_recipients_status"), table_name="platform_email_campaign_recipients")
    op.drop_index(op.f("ix_platform_email_campaign_recipients_id"), table_name="platform_email_campaign_recipients")
    op.drop_index(op.f("ix_platform_email_campaign_recipients_campaign_id"), table_name="platform_email_campaign_recipients")
    op.drop_index("ix_platform_email_campaign_recipients_email_campaign", table_name="platform_email_campaign_recipients")
    op.drop_index("ix_platform_email_campaign_recipients_campaign_status", table_name="platform_email_campaign_recipients")
    op.drop_table("platform_email_campaign_recipients")

    op.drop_index(op.f("ix_platform_email_campaigns_audience_type"), table_name="platform_email_campaigns")
    op.drop_index(op.f("ix_platform_email_campaigns_status"), table_name="platform_email_campaigns")
    op.drop_index("ix_platform_email_campaigns_status_created", table_name="platform_email_campaigns")
    op.drop_index(op.f("ix_platform_email_campaigns_id"), table_name="platform_email_campaigns")
    op.drop_index(op.f("ix_platform_email_campaigns_created_by_admin_id"), table_name="platform_email_campaigns")
    op.drop_index("ix_platform_email_campaigns_admin_created", table_name="platform_email_campaigns")
    op.drop_table("platform_email_campaigns")
