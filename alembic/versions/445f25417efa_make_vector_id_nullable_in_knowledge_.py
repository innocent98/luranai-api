"""Make vector_id nullable in knowledge_chunks

Revision ID: 445f25417efa
Revises: 7d8e9f0a2b43
Create Date: 2026-01-27 02:57:09.859285

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '445f25417efa'
down_revision = '7d8e9f0a2b43'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make vector_id nullable (allows chunks to be created before embeddings are generated)
    op.alter_column('knowledge_chunks', 'vector_id',
                    existing_type=sa.String(100),
                    nullable=True)


def downgrade() -> None:
    # Revert to NOT NULL (this may fail if there are NULL values)
    op.alter_column('knowledge_chunks', 'vector_id',
                    existing_type=sa.String(100),
                    nullable=False)
