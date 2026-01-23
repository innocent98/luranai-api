"""add knowledge_source version constraint

Revision ID: 5c6d8e9f0a21
Revises: 4b5c7d8e9f10
Create Date: 2026-01-23 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5c6d8e9f0a21'
down_revision = '4b5c7d8e9f10'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add unique constraint to prevent race conditions in version creation
    # Only one version per (tenant_id, content_hash, version) tuple
    op.create_unique_constraint(
        'uq_knowledge_source_tenant_hash_version',
        'knowledge_sources',
        ['tenant_id', 'content_hash', 'version']
    )


def downgrade() -> None:
    # Remove unique constraint
    op.drop_constraint(
        'uq_knowledge_source_tenant_hash_version',
        'knowledge_sources',
        type_='unique'
    )
