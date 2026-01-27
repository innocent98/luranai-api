"""Add payment transactions table

Revision ID: 1f5ce7a406dd
Revises: 445f25417efa
Create Date: 2026-01-27 10:31:58.253850

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '1f5ce7a406dd'
down_revision = '445f25417efa'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create payment_transactions table
    op.create_table(
        'payment_transactions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reference', sa.String(100), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, server_default='NGN'),
        sa.Column('status', sa.String(50), nullable=False, server_default='pending'),
        sa.Column('tier', sa.String(50), nullable=False),
        sa.Column('billing_cycle', sa.String(20), nullable=False),
        sa.Column('customer_email', sa.String(255), nullable=True),
        sa.Column('customer_code', sa.String(100), nullable=True),
        sa.Column('channel', sa.String(50), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('subscription_code', sa.String(100), nullable=True),
        sa.Column('authorization_code', sa.String(100), nullable=True),
        sa.Column('paystack_data', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )

    # Create indexes
    op.create_index('ix_payment_transactions_id', 'payment_transactions', ['id'])
    op.create_index('ix_payment_transactions_tenant_id', 'payment_transactions', ['tenant_id'])
    op.create_index('ix_payment_transactions_reference', 'payment_transactions', ['reference'], unique=True)
    op.create_index('ix_payment_transactions_status', 'payment_transactions', ['status'])
    op.create_index('ix_payment_transactions_subscription_code', 'payment_transactions', ['subscription_code'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('ix_payment_transactions_subscription_code', 'payment_transactions')
    op.drop_index('ix_payment_transactions_status', 'payment_transactions')
    op.drop_index('ix_payment_transactions_reference', 'payment_transactions')
    op.drop_index('ix_payment_transactions_tenant_id', 'payment_transactions')
    op.drop_index('ix_payment_transactions_id', 'payment_transactions')

    # Drop table
    op.drop_table('payment_transactions')
