"""add lookup chain capability

Revision ID: y2z3a4b5c6d7
Revises: x1y2z3a4b5c6
Create Date: 2026-03-27 14:05:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "y2z3a4b5c6d7"
down_revision = "x1y2z3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_plans",
        sa.Column("can_use_lookup_chains", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tenant_capabilities",
        sa.Column("can_use_lookup_chains", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.execute(
        """
        UPDATE subscription_plans
        SET can_use_lookup_chains = CASE
            WHEN tier IN ('professional', 'growth', 'enterprise') THEN TRUE
            ELSE FALSE
        END
        """
    )
    op.execute(
        """
        UPDATE tenant_capabilities AS tc
        SET can_use_lookup_chains = COALESCE(sp.can_use_lookup_chains, FALSE)
        FROM tenants AS t
        LEFT JOIN subscription_plans AS sp ON sp.tier = t.subscription_tier
        WHERE tc.tenant_id = t.id
        """
    )

    op.alter_column("subscription_plans", "can_use_lookup_chains", server_default=None)
    op.alter_column("tenant_capabilities", "can_use_lookup_chains", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "can_use_lookup_chains")
    op.drop_column("subscription_plans", "can_use_lookup_chains")
