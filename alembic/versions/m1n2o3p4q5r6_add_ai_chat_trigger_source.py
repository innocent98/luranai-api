"""add ai chat trigger source

Revision ID: m1n2o3p4q5r6
Revises: l7m8n9o0p1q2
Create Date: 2026-03-13 15:10:00.000000
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "m1n2o3p4q5r6"
down_revision = "l7m8n9o0p1q2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TYPE action_trigger_source ADD VALUE IF NOT EXISTS 'ai_chat';
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
        """
    )


def downgrade() -> None:
    # PostgreSQL enum values are not safely removable in-place.
    pass
