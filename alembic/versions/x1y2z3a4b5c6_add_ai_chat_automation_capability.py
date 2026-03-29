"""add ai chat automation capability

Revision ID: x1y2z3a4b5c6
Revises: v0w1x2y3z4a5
Create Date: 2026-03-27 13:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "x1y2z3a4b5c6"
down_revision = "v0w1x2y3z4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_plans",
        sa.Column("can_use_ai_chat_automation", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tenant_capabilities",
        sa.Column("can_use_ai_chat_automation", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.execute(
        """
        UPDATE subscription_plans
        SET can_use_ai_chat_automation = TRUE
        """
    )
    op.execute(
        """
        UPDATE tenant_capabilities AS tc
        SET can_use_ai_chat_automation = COALESCE(sp.can_use_ai_chat_automation, TRUE)
        FROM tenants AS t
        LEFT JOIN subscription_plans AS sp ON sp.tier = t.subscription_tier
        WHERE tc.tenant_id = t.id
        """
    )

    op.alter_column("subscription_plans", "can_use_ai_chat_automation", server_default=None)
    op.alter_column("tenant_capabilities", "can_use_ai_chat_automation", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "can_use_ai_chat_automation")
    op.drop_column("subscription_plans", "can_use_ai_chat_automation")
