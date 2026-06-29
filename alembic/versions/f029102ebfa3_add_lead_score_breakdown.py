"""add lead score_breakdown

Revision ID: f029102ebfa3
Revises: e8f9g0h1i2j3
Create Date: 2026-06-24 12:23:02.755391

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f029102ebfa3'
down_revision = 'e8f9g0h1i2j3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('leads', sa.Column('score_breakdown', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('leads', 'score_breakdown')
