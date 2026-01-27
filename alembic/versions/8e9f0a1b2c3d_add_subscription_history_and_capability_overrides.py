"""Add subscription history and capability overrides

Revision ID: 8e9f0a1b2c3d
Revises: 1f5ce7a406dd
Create Date: 2026-01-27 11:35:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '8e9f0a1b2c3d'
down_revision = '1f5ce7a406dd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create subscription_history table
    op.create_table(
        'subscription_history',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('previous_tier', sa.String(50), nullable=True),
        sa.Column('new_tier', sa.String(50), nullable=True),
        sa.Column('previous_status', sa.String(50), nullable=True),
        sa.Column('new_status', sa.String(50), nullable=True),
        sa.Column('changed_by_type', sa.String(20), nullable=False),
        sa.Column('changed_by_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('changed_by_name', sa.String(255), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('event_metadata', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('payment_reference', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE')
    )

    # Create indexes for subscription_history
    op.create_index('ix_subscription_history_id', 'subscription_history', ['id'])
    op.create_index('ix_subscription_history_tenant_id', 'subscription_history', ['tenant_id'])
    op.create_index('ix_subscription_history_payment_reference', 'subscription_history', ['payment_reference'])

    # 2. Add override_flags column to tenant_capabilities
    op.add_column(
        'tenant_capabilities',
        sa.Column('override_flags', postgresql.JSON(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    # Remove override_flags column from tenant_capabilities
    op.drop_column('tenant_capabilities', 'override_flags')

    # Drop indexes for subscription_history
    op.drop_index('ix_subscription_history_payment_reference', 'subscription_history')
    op.drop_index('ix_subscription_history_tenant_id', 'subscription_history')
    op.drop_index('ix_subscription_history_id', 'subscription_history')

    # Drop subscription_history table
    op.drop_table('subscription_history')
