"""add webhook fields to tenant capabilities

Revision ID: z3a4b5c6d7e8
Revises: y2z3a4b5c6d7
Create Date: 2026-03-27 12:05:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "z3a4b5c6d7e8"
down_revision = "y2z3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_capabilities",
        sa.Column("can_use_webhooks", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tenant_capabilities",
        sa.Column("max_webhooks_per_flow", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE tenant_capabilities tc
        SET
            can_use_webhooks = sp.can_use_webhooks,
            max_webhooks_per_flow = sp.max_webhooks_per_flow
        FROM tenants t
        JOIN subscription_plans sp ON sp.tier = t.subscription_tier
        WHERE tc.tenant_id = t.id
        """
    )
    op.alter_column("tenant_capabilities", "can_use_webhooks", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "max_webhooks_per_flow")
    op.drop_column("tenant_capabilities", "can_use_webhooks")
