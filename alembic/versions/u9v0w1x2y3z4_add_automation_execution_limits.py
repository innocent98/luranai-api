"""add automation execution limits to plans and tenant capabilities

Revision ID: u9v0w1x2y3z4
Revises: p3q4r5s6t7u8
Create Date: 2026-03-27 18:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "u9v0w1x2y3z4"
down_revision = "p3q4r5s6t7u8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_plans",
        sa.Column("max_automation_executions_per_month", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tenant_capabilities",
        sa.Column("max_automation_executions_per_month", sa.Integer(), nullable=True),
    )

    op.execute(
        """
        UPDATE subscription_plans
        SET max_automation_executions_per_month = CASE tier
            WHEN 'trial' THEN 100
            WHEN 'lite' THEN 500
            WHEN 'professional' THEN 5000
            WHEN 'growth' THEN 25000
            WHEN 'enterprise' THEN NULL
            ELSE max_automation_executions_per_month
        END
        """
    )

    op.execute(
        """
        UPDATE tenant_capabilities tc
        SET max_automation_executions_per_month = sp.max_automation_executions_per_month
        FROM tenants t
        JOIN subscription_plans sp ON sp.tier = t.subscription_tier
        WHERE tc.tenant_id = t.id
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "max_automation_executions_per_month")
    op.drop_column("subscription_plans", "max_automation_executions_per_month")
