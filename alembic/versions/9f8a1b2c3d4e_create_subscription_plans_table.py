"""create_subscription_plans_table

Revision ID: 9f8a1b2c3d4e
Revises: 3081c68284fa
Create Date: 2026-01-27 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '9f8a1b2c3d4e'
down_revision = '8e9f0a1b2c3d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create subscription_plans table
    op.create_table(
        'subscription_plans',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, primary_key=True, index=True),

        # Identification
        sa.Column('tier', sa.String(length=50), nullable=False, unique=True, index=True, comment='Plan tier: trial, lite, professional, enterprise'),
        sa.Column('display_name', sa.String(length=100), nullable=False, comment='Display name for UI'),
        sa.Column('description', sa.String(length=500), nullable=False, comment='Plan description'),

        # Pricing (all in kobo - Nigerian currency subunit)
        sa.Column('monthly_price_kobo', sa.Integer(), nullable=False, comment='Monthly price in kobo (1 naira = 100 kobo)'),
        sa.Column('annual_price_kobo', sa.Integer(), nullable=False, comment='Annual price in kobo'),

        # Capabilities (matching TIER_CONFIGS structure)
        sa.Column('can_access_analytics', sa.Boolean(), nullable=False, server_default='false', comment='Whether tier can access analytics dashboard'),
        sa.Column('max_knowledge_sources', sa.Integer(), nullable=True, comment='Maximum knowledge sources (NULL = unlimited)'),
        sa.Column('can_use_url_sources', sa.Boolean(), nullable=False, server_default='false', comment='Whether tier can use URL knowledge sources'),
        sa.Column('max_knowledge_mb', sa.Integer(), nullable=True, comment='Maximum knowledge storage in MB (NULL = unlimited)'),
        sa.Column('max_conversations_per_month', sa.Integer(), nullable=True, comment='Maximum conversations per month (NULL = unlimited)'),
        sa.Column('max_team_members', sa.Integer(), nullable=True, comment='Maximum team members (NULL = unlimited)'),
        sa.Column('max_response_tokens', sa.Integer(), nullable=False, comment='Maximum tokens per AI response'),

        # Marketing
        sa.Column('features', postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default='[]', comment='List of feature descriptions for marketing'),

        # Status
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true', index=True, comment='Whether plan is active/visible'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0', index=True, comment='Display order (lower = first)'),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Creation timestamp'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Last update timestamp'),

        sa.PrimaryKeyConstraint('id'),
    )

    # Create indexes (id index auto-created by UUIDMixin, tier index auto-created by unique=True)
    # Composite index for filtering active plans by sort order
    op.create_index('ix_subscription_plans_active_sort', 'subscription_plans', ['is_active', 'sort_order'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('ix_subscription_plans_active_sort', table_name='subscription_plans')

    # Drop table (will cascade drop other indexes)
    op.drop_table('subscription_plans')
