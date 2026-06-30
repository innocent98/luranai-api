"""add notification_logs

Revision ID: d76946bef467
Revises: 829aa440b93e
Create Date: 2026-06-29 17:20:21.816357

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd76946bef467'
down_revision = '829aa440b93e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "event",
            sa.Enum(
                "sales_ready_lead", "meeting_booked", "quote_generated",
                name="notif_event",
            ),
            nullable=False,
        ),
        sa.Column(
            "channel",
            sa.Enum(
                "email", "in_app", "slack", "crm",
                name="notif_channel",
            ),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "sent", "failed",
                name="notif_status",
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notification_logs_id", "notification_logs", ["id"])
    op.create_index("ix_notification_logs_tenant_id", "notification_logs", ["tenant_id"])
    op.create_index("ix_notification_logs_entity_id", "notification_logs", ["entity_id"])
    op.create_index(
        "uq_notification_dedupe",
        "notification_logs",
        ["tenant_id", "event", "channel", "entity_id", "recipient"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_notification_dedupe", table_name="notification_logs")
    op.drop_index("ix_notification_logs_entity_id", table_name="notification_logs")
    op.drop_index("ix_notification_logs_tenant_id", table_name="notification_logs")
    op.drop_index("ix_notification_logs_id", table_name="notification_logs")
    op.drop_table("notification_logs")
    sa.Enum(name="notif_event").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="notif_channel").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="notif_status").drop(op.get_bind(), checkfirst=True)
