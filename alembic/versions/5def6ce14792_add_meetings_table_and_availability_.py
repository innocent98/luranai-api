"""add meetings table and availability config

Revision ID: 5def6ce14792
Revises: 145010cdeb0e
Create Date: 2026-06-29 12:37:52.289911

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5def6ce14792'
down_revision = '145010cdeb0e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meetings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("visitor_timezone", sa.String(length=64), nullable=True),
        sa.Column("use_case", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("scheduled", "cancelled", name="meeting_status"),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meetings_id", "meetings", ["id"])
    op.create_index("ix_meetings_tenant_id", "meetings", ["tenant_id"])
    op.create_index("ix_meetings_lead_id", "meetings", ["lead_id"])
    op.create_index("ix_meetings_conversation_id", "meetings", ["conversation_id"])
    op.create_index(
        "uq_meeting_tenant_slot_scheduled",
        "meetings",
        ["tenant_id", "scheduled_at"],
        unique=True,
        postgresql_where=sa.text("status = 'scheduled'"),
    )
    # Add as nullable first to backfill existing rows safely (avoids :n bind-param
    # substitution issues that arise when embedding integer values in JSON literals
    # inside sa.text() server_default expressions).
    op.add_column(
        "tenant_sales_agent_config",
        sa.Column("availability", sa.JSON(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE tenant_sales_agent_config SET availability = :v WHERE availability IS NULL"
        ).bindparams(
            v='{"weekdays":[1,2,3,4,5],"start":"09:00","end":"17:00",'
              '"slot_minutes":30,"buffer_minutes":15,"tenant_timezone":"UTC"}'
        )
    )
    op.alter_column("tenant_sales_agent_config", "availability", nullable=False)


def downgrade() -> None:
    op.drop_column("tenant_sales_agent_config", "availability")
    op.drop_index("uq_meeting_tenant_slot_scheduled", table_name="meetings")
    op.drop_index("ix_meetings_conversation_id", table_name="meetings")
    op.drop_index("ix_meetings_lead_id", table_name="meetings")
    op.drop_index("ix_meetings_tenant_id", table_name="meetings")
    op.drop_index("ix_meetings_id", table_name="meetings")
    op.drop_table("meetings")
    sa.Enum(name="meeting_status").drop(op.get_bind(), checkfirst=True)
