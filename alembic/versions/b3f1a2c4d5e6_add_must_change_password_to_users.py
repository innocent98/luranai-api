"""add must_change_password to users

Revision ID: b3f1a2c4d5e6
Revises: 78f03ae0ee64
Create Date: 2026-04-09

"""
from alembic import op
import sqlalchemy as sa

revision = 'b3f1a2c4d5e6'
down_revision = '78f03ae0ee64'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('users', 'must_change_password')
