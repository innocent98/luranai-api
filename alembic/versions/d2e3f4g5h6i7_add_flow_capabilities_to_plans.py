"""add_flow_capabilities_to_plans

Revision ID: d2e3f4g5h6i7
Revises: c1d2e3f4g5h6
Create Date: 2026-02-04

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd2e3f4g5h6i7'
down_revision = 'c1d2e3f4g5h6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add flow capability columns to subscription_plans table."""

    # Add flow capability columns
    op.add_column('subscription_plans',
        sa.Column('max_flows', sa.Integer(), nullable=True,
                  comment='Maximum number of flows allowed (NULL = unlimited)'))
    op.add_column('subscription_plans',
        sa.Column('max_nodes_per_flow', sa.Integer(), nullable=True,
                  comment='Maximum nodes per flow (NULL = unlimited)'))
    op.add_column('subscription_plans',
        sa.Column('can_use_webhooks', sa.Boolean(),
                  server_default='false', nullable=False,
                  comment='Whether webhooks are allowed'))
    op.add_column('subscription_plans',
        sa.Column('max_webhooks_per_flow', sa.Integer(), nullable=True,
                  comment='Maximum webhooks per flow (NULL = unlimited)'))


def downgrade() -> None:
    """Remove flow capability columns from subscription_plans table."""

    # Drop columns in reverse order
    op.drop_column('subscription_plans', 'max_webhooks_per_flow')
    op.drop_column('subscription_plans', 'can_use_webhooks')
    op.drop_column('subscription_plans', 'max_nodes_per_flow')
    op.drop_column('subscription_plans', 'max_flows')
