"""add WEBSITE_AUDIT to jobtype enum

Revision ID: e8f9g0h1i2j3
Revises: d5e6f7g8h9i0
Create Date: 2026-06-20 00:00:00.000000
"""
from alembic import op

revision = "e8f9g0h1i2j3"
down_revision = "d5e6f7g8h9i0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'WEBSITE_AUDIT'")


def downgrade() -> None:
    # Postgres does not support removing enum values safely; this is a no-op.
    pass
