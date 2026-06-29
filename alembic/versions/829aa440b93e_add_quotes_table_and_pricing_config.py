"""add quotes table and pricing config

Revision ID: 829aa440b93e
Revises: 5def6ce14792
Create Date: 2026-06-29 16:17:01.922302

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '829aa440b93e'
down_revision = '5def6ce14792'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quotes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("line_items", sa.JSON(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "sent", name="quote_status"),
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
    op.create_index("ix_quotes_id", "quotes", ["id"])
    op.create_index("ix_quotes_tenant_id", "quotes", ["tenant_id"])
    op.create_index("ix_quotes_lead_id", "quotes", ["lead_id"])
    op.create_index("ix_quotes_conversation_id", "quotes", ["conversation_id"])

    op.add_column("leads", sa.Column("quote_id", sa.UUID(), nullable=True))

    # Add as nullable first to avoid issues with existing rows, then drop server_default
    op.add_column(
        "tenant_sales_agent_config",
        sa.Column("annual_discount_pct", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE tenant_sales_agent_config SET annual_discount_pct = 0 "
            "WHERE annual_discount_pct IS NULL"
        )
    )
    op.alter_column(
        "tenant_sales_agent_config",
        "annual_discount_pct",
        nullable=False,
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("tenant_sales_agent_config", "annual_discount_pct")
    op.drop_column("leads", "quote_id")
    op.drop_index("ix_quotes_conversation_id", table_name="quotes")
    op.drop_index("ix_quotes_lead_id", table_name="quotes")
    op.drop_index("ix_quotes_tenant_id", table_name="quotes")
    op.drop_index("ix_quotes_id", table_name="quotes")
    op.drop_table("quotes")
    sa.Enum(name="quote_status").drop(op.get_bind(), checkfirst=True)
