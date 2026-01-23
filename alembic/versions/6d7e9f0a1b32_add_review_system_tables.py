"""Add review system tables

Revision ID: 6d7e9f0a1b32
Revises: 5c6d8e9f0a21
Create Date: 2026-01-23 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '6d7e9f0a1b32'
down_revision = '5c6d8e9f0a21'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create session_ratings and platform_feedback tables."""

    # Create session_ratings table
    op.create_table(
        'session_ratings',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False, comment='Tenant this rating belongs to'),
        sa.Column('session_id', sa.Uuid(), nullable=False, comment='Conversation session being rated'),
        sa.Column('score', sa.Integer(), nullable=False, comment='Rating score 1-5 (1=Poor, 2=Fair, 3=Good, 4=Very Good, 5=Excellent)'),
        sa.Column('comment', sa.Text(), nullable=True, comment='Optional feedback comment from customer'),
        sa.Column('user_identifier', sa.String(length=255), nullable=False, comment='User who submitted rating (for verification)'),
        sa.Column('meta', sa.JSON(), nullable=True, comment='Additional context: {ai_helpful, issues_faced, etc.}'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['conversation_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id', name='uq_session_rating_per_session')
    )

    # Create indexes for session_ratings
    op.create_index(op.f('ix_session_ratings_id'), 'session_ratings', ['id'], unique=False)
    op.create_index(op.f('ix_session_ratings_tenant_id'), 'session_ratings', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_session_ratings_session_id'), 'session_ratings', ['session_id'], unique=True)
    op.create_index('ix_session_ratings_tenant_session', 'session_ratings', ['tenant_id', 'session_id'], unique=False)
    op.create_index('ix_session_ratings_tenant_created', 'session_ratings', ['tenant_id', 'created_at'], unique=False)
    op.create_index('ix_session_ratings_score', 'session_ratings', ['tenant_id', 'score'], unique=False)

    # Create updated_at trigger for session_ratings
    op.execute("""
        CREATE OR REPLACE FUNCTION update_session_ratings_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    op.execute("""
        CREATE TRIGGER update_session_ratings_updated_at
            BEFORE UPDATE ON session_ratings
            FOR EACH ROW
            EXECUTE FUNCTION update_session_ratings_updated_at();
    """)

    # Create platform_feedback table (enum will be created automatically by SQLAlchemy)
    op.create_table(
        'platform_feedback',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False, comment='Tenant providing feedback'),
        sa.Column('category', sa.Enum('feature_request', 'bug_report', 'performance', 'platform_satisfaction', 'documentation', 'pricing', 'general', name='feedback_category'), nullable=False, comment='Type of feedback'),
        sa.Column('satisfaction_score', sa.Integer(), nullable=True, comment='1-5 rating (only for platform_satisfaction category)'),
        sa.Column('title', sa.String(length=500), nullable=False, comment='Feedback title/summary'),
        sa.Column('description', sa.Text(), nullable=False, comment='Detailed feedback description'),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='open', comment='Status: open, acknowledged, in_progress, resolved, closed'),
        sa.Column('submitted_by', sa.Uuid(), nullable=True, comment='User who submitted feedback'),
        sa.Column('meta', sa.JSON(), nullable=True, comment='Additional context: {priority, affected_features, etc.}'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['submitted_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes for platform_feedback
    op.create_index(op.f('ix_platform_feedback_id'), 'platform_feedback', ['id'], unique=False)
    op.create_index(op.f('ix_platform_feedback_tenant_id'), 'platform_feedback', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_platform_feedback_category'), 'platform_feedback', ['category'], unique=False)
    op.create_index(op.f('ix_platform_feedback_status'), 'platform_feedback', ['status'], unique=False)
    op.create_index('ix_platform_feedback_tenant_category', 'platform_feedback', ['tenant_id', 'category'], unique=False)
    op.create_index('ix_platform_feedback_tenant_created', 'platform_feedback', ['tenant_id', 'created_at'], unique=False)

    # Create updated_at trigger for platform_feedback
    op.execute("""
        CREATE OR REPLACE FUNCTION update_platform_feedback_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    op.execute("""
        CREATE TRIGGER update_platform_feedback_updated_at
            BEFORE UPDATE ON platform_feedback
            FOR EACH ROW
            EXECUTE FUNCTION update_platform_feedback_updated_at();
    """)


def downgrade() -> None:
    """Drop session_ratings and platform_feedback tables."""

    # Drop platform_feedback triggers and table
    op.execute("DROP TRIGGER IF EXISTS update_platform_feedback_updated_at ON platform_feedback")
    op.execute("DROP FUNCTION IF EXISTS update_platform_feedback_updated_at()")

    op.drop_index('ix_platform_feedback_tenant_created', table_name='platform_feedback')
    op.drop_index('ix_platform_feedback_tenant_category', table_name='platform_feedback')
    op.drop_index(op.f('ix_platform_feedback_status'), table_name='platform_feedback')
    op.drop_index(op.f('ix_platform_feedback_category'), table_name='platform_feedback')
    op.drop_index(op.f('ix_platform_feedback_tenant_id'), table_name='platform_feedback')
    op.drop_index(op.f('ix_platform_feedback_id'), table_name='platform_feedback')
    op.drop_table('platform_feedback')

    # Drop session_ratings triggers and table
    op.execute("DROP TRIGGER IF EXISTS update_session_ratings_updated_at ON session_ratings")
    op.execute("DROP FUNCTION IF EXISTS update_session_ratings_updated_at()")

    op.drop_index('ix_session_ratings_score', table_name='session_ratings')
    op.drop_index('ix_session_ratings_tenant_created', table_name='session_ratings')
    op.drop_index('ix_session_ratings_tenant_session', table_name='session_ratings')
    op.drop_index(op.f('ix_session_ratings_session_id'), table_name='session_ratings')
    op.drop_index(op.f('ix_session_ratings_tenant_id'), table_name='session_ratings')
    op.drop_index(op.f('ix_session_ratings_id'), table_name='session_ratings')
    op.drop_table('session_ratings')

    # Drop enum type
    op.execute("DROP TYPE IF EXISTS feedback_category")
