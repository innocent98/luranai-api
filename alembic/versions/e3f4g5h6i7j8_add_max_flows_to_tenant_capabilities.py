"""add_max_flows_to_tenant_capabilities

Revision ID: e3f4g5h6i7j8
Revises: d2e3f4g5h6i7
Create Date: 2026-02-10

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e3f4g5h6i7j8"
down_revision = "d2e3f4g5h6i7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add max_flows to tenant_capabilities and backfill from subscription tier."""
    op.add_column(
        "tenant_capabilities",
        sa.Column(
            "max_flows",
            sa.Integer(),
            nullable=True,
            comment="Maximum flows allowed (NULL = unlimited)",
        ),
    )

    # Backfill from each tenant's current subscription tier plan.
    op.execute(
        """
        UPDATE tenant_capabilities tc
        SET max_flows = sp.max_flows
        FROM tenants t
        JOIN subscription_plans sp ON sp.tier = t.subscription_tier
        WHERE tc.tenant_id = t.id
        """
    )


def downgrade() -> None:
    """Remove max_flows from tenant_capabilities."""
    op.drop_column("tenant_capabilities", "max_flows")
