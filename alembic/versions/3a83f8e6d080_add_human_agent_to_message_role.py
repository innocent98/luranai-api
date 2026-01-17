"""add_human_agent_to_message_role

Revision ID: 3a83f8e6d080
Revises: f3d55906a9b1
Create Date: 2026-01-17 01:59:42.502784

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3a83f8e6d080'
down_revision = 'f3d55906a9b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add 'human_agent' value to message_role enum
    # Note: In PostgreSQL, enum values cannot be added in a transaction,
    # so we need to execute this outside of a transaction block
    op.execute("ALTER TYPE message_role ADD VALUE 'human_agent'")


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly
    # You would need to recreate the enum type to remove a value
    # For simplicity, we'll leave this as a no-op since downgrade is rarely used
    pass
