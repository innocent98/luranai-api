"""add tenant automation config

Revision ID: l7m8n9o0p1q2
Revises: k1l2m3n4o5p6
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "l7m8n9o0p1q2"
down_revision = "k1l2m3n4o5p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_automation_configs",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("api_base_url", sa.Text(), nullable=True),
        sa.Column("default_runtime_variables", sa.JSON(), nullable=False),
        sa.Column("lookup_action_mapping", sa.JSON(), nullable=False),
        sa.Column("identifier_mapping", sa.JSON(), nullable=False),
        sa.Column("output_mapping", sa.JSON(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tenant_automation_configs_tenant_id",
        "tenant_automation_configs",
        ["tenant_id"],
        unique=True,
    )
    op.create_index(
        "ix_tenant_automation_configs_id",
        "tenant_automation_configs",
        ["id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_automation_configs_id", table_name="tenant_automation_configs")
    op.drop_index("ix_tenant_automation_configs_tenant_id", table_name="tenant_automation_configs")
    op.drop_table("tenant_automation_configs")
