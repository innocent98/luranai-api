"""add action secrets and execution controls

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "k1l2m3n4o5p6"
down_revision = "j0k1l2m3n4o5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_secrets",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("key_name", sa.String(length=120), nullable=False),
        sa.Column("secret_value", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_secrets_id", "action_secrets", ["id"], unique=False)
    op.create_index("ix_action_secrets_tenant_id", "action_secrets", ["tenant_id"], unique=False)
    op.create_index(
        "ix_action_secrets_tenant_provider_key",
        "action_secrets",
        ["tenant_id", "provider", "key_name"],
        unique=True,
    )
    op.create_index(
        "ix_action_secrets_tenant_status",
        "action_secrets",
        ["tenant_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_action_secrets_tenant_status", table_name="action_secrets")
    op.drop_index("ix_action_secrets_tenant_provider_key", table_name="action_secrets")
    op.drop_index("ix_action_secrets_tenant_id", table_name="action_secrets")
    op.drop_index("ix_action_secrets_id", table_name="action_secrets")
    op.drop_table("action_secrets")
