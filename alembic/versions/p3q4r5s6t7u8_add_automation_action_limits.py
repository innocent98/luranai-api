"""add automation action limits to plans and tenant capabilities

Revision ID: p3q4r5s6t7u8
Revises: n2o3p4q5r6s7
Create Date: 2026-03-27 13:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "p3q4r5s6t7u8"
down_revision = "n2o3p4q5r6s7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_plans",
        sa.Column("max_automation_actions", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tenant_capabilities",
        sa.Column("max_automation_actions", sa.Integer(), nullable=True),
    )

    op.execute(
        """
        UPDATE subscription_plans
        SET max_automation_actions = CASE tier
            WHEN 'trial' THEN 2
            WHEN 'lite' THEN 5
            WHEN 'professional' THEN 15
            WHEN 'growth' THEN 40
            WHEN 'enterprise' THEN NULL
            ELSE max_automation_actions
        END
        """
    )

    op.execute(
        """
        UPDATE subscription_plans
        SET max_flows = CASE tier
            WHEN 'trial' THEN 1
            WHEN 'lite' THEN 3
            WHEN 'professional' THEN 10
            WHEN 'growth' THEN 25
            WHEN 'enterprise' THEN NULL
            ELSE max_flows
        END
        """
    )

    op.execute(
        """
        UPDATE tenant_capabilities tc
        SET
            max_automation_actions = sp.max_automation_actions,
            max_flows = sp.max_flows
        FROM tenants t
        JOIN subscription_plans sp ON sp.tier = t.subscription_tier
        WHERE tc.tenant_id = t.id
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "max_automation_actions")
    op.drop_column("subscription_plans", "max_automation_actions")
