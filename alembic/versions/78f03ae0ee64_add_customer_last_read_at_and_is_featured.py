"""add customer_last_read_at and is_featured columns

Revision ID: 78f03ae0ee64
Revises: c6d7e8f9a0b1
Create Date: 2026-04-06

"""
from alembic import op
import sqlalchemy as sa

revision = '78f03ae0ee64'
down_revision = 'c6d7e8f9a0b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'conversation_sessions',
        sa.Column('customer_last_read_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'tenants',
        sa.Column('is_featured', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.create_index('ix_tenants_is_featured', 'tenants', ['is_featured'])


def downgrade() -> None:
    op.drop_index('ix_tenants_is_featured', 'tenants')
    op.drop_column('tenants', 'is_featured')
    op.drop_column('conversation_sessions', 'customer_last_read_at')
