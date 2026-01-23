"""remove confidence_score columns

Revision ID: 4b5c7d8e9f10
Revises: 3a83f8e6d080
Create Date: 2026-01-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '4b5c7d8e9f10'
down_revision = '3a83f8e6d080'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove confidence_score from messages table
    op.drop_column('messages', 'confidence_score')

    # Remove confidence_scores from conversation_sessions table
    op.drop_column('conversation_sessions', 'confidence_scores')


def downgrade() -> None:
    # Restore columns if needed (for rollback)
    op.add_column('conversation_sessions',
                  sa.Column('confidence_scores', postgresql.JSON(astext_type=sa.Text()), nullable=True))

    op.add_column('messages',
                  sa.Column('confidence_score', sa.Float(), nullable=True))
