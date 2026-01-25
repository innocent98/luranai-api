"""Add public_feedback table

Revision ID: 2c83e2374c57
Revises: 6d7e9f0a1b32
Create Date: 2026-01-25 01:21:31.322435

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '2c83e2374c57'
down_revision = '6d7e9f0a1b32'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create public_feedback table using existing feedback_category enum
    op.create_table('public_feedback',
        sa.Column('category', postgresql.ENUM('feature_request', 'bug_report', 'performance', 'platform_satisfaction', 'documentation', 'pricing', 'general', name='feedback_category', create_type=False), nullable=False, comment='Type of feedback'),
        sa.Column('satisfaction_score', sa.Integer(), nullable=True, comment='1-5 rating (optional)'),
        sa.Column('title', sa.String(length=500), nullable=False, comment='Feedback title/summary'),
        sa.Column('description', sa.Text(), nullable=False, comment='Detailed feedback description'),
        sa.Column('name', sa.String(length=255), nullable=True, comment='Submitter name (optional)'),
        sa.Column('email', sa.String(length=255), nullable=True, comment='Contact email for follow-up (optional)'),
        sa.Column('company', sa.String(length=255), nullable=True, comment='Company/organization name (optional)'),
        sa.Column('status', sa.String(length=50), nullable=False, comment='Status: open, acknowledged, in_progress, resolved, closed'),
        sa.Column('meta', sa.JSON(), nullable=True, comment='Additional context: {referral_source, page_url, user_agent, etc.}'),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes
    op.create_index(op.f('ix_public_feedback_category'), 'public_feedback', ['category'], unique=False)
    op.create_index('ix_public_feedback_category_created', 'public_feedback', ['category', 'created_at'], unique=False)
    op.create_index('ix_public_feedback_email', 'public_feedback', ['email'], unique=False)
    op.create_index(op.f('ix_public_feedback_id'), 'public_feedback', ['id'], unique=False)
    op.create_index(op.f('ix_public_feedback_status'), 'public_feedback', ['status'], unique=False)


def downgrade() -> None:
    # Drop indexes
    op.drop_index(op.f('ix_public_feedback_status'), table_name='public_feedback')
    op.drop_index(op.f('ix_public_feedback_id'), table_name='public_feedback')
    op.drop_index('ix_public_feedback_email', table_name='public_feedback')
    op.drop_index('ix_public_feedback_category_created', table_name='public_feedback')
    op.drop_index(op.f('ix_public_feedback_category'), table_name='public_feedback')

    # Drop table
    op.drop_table('public_feedback')
