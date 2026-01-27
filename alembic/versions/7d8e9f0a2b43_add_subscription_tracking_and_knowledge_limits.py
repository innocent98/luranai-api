"""Add subscription tracking and knowledge limits

Revision ID: 7d8e9f0a2b43
Revises: 2c83e2374c57
Create Date: 2026-01-27 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime, timedelta

# revision identifiers, used by Alembic.
revision = '7d8e9f0a2b43'
down_revision = '2c83e2374c57'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add subscription tracking and knowledge source limits."""

    # 1. Add subscription fields to tenants table
    op.add_column('tenants',
        sa.Column('subscription_tier', sa.String(50),
                  server_default='trial', nullable=False,
                  comment='Subscription tier: trial, lite, professional, enterprise'))
    op.add_column('tenants',
        sa.Column('subscription_status', sa.String(50),
                  server_default='trialing', nullable=False,
                  comment='Status: trialing, active, past_due, canceled, trial_expired'))
    op.add_column('tenants',
        sa.Column('billing_cycle', sa.String(20), nullable=True,
                  comment='Billing cycle: monthly, annual (null for trial)'))
    op.add_column('tenants',
        sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True,
                  comment='When 30-day trial expires (set on signup)'))
    op.add_column('tenants',
        sa.Column('subscription_starts_at', sa.DateTime(timezone=True), nullable=True,
                  comment='When paid subscription started'))
    op.add_column('tenants',
        sa.Column('subscription_ends_at', sa.DateTime(timezone=True), nullable=True,
                  comment='When subscription ends (canceled or expired)'))

    # Create indexes for subscription fields
    op.create_index('ix_tenants_subscription_tier', 'tenants', ['subscription_tier'])
    op.create_index('ix_tenants_subscription_status', 'tenants', ['subscription_status'])
    op.create_index('ix_tenants_trial_ends_at', 'tenants', ['trial_ends_at'])

    # 2. Set trial_ends_at for existing tenants (30 days from created_at)
    # Using raw SQL for PostgreSQL interval
    op.execute("""
        UPDATE tenants
        SET trial_ends_at = created_at + INTERVAL '30 days'
        WHERE trial_ends_at IS NULL
    """)

    # 3. Add capability fields to tenant_capabilities
    op.add_column('tenant_capabilities',
        sa.Column('max_knowledge_sources', sa.Integer(), nullable=True,
                  comment='Total knowledge sources: files + URLs combined (null = unlimited)'))
    op.add_column('tenant_capabilities',
        sa.Column('can_use_url_sources', sa.Boolean(),
                  server_default='false', nullable=False,
                  comment='Can scrape webpage URLs as knowledge sources'))
    op.add_column('tenant_capabilities',
        sa.Column('max_team_members', sa.Integer(), nullable=True,
                  comment='Maximum team members allowed (null = unlimited)'))

    # Set defaults for existing tenants (trial tier defaults)
    op.execute("""
        UPDATE tenant_capabilities
        SET max_knowledge_sources = 2,
            can_use_url_sources = false,
            max_team_members = 1
        WHERE max_knowledge_sources IS NULL
    """)

    # 4. Add source tracking fields to knowledge_sources
    op.add_column('knowledge_sources',
        sa.Column('source_category', sa.String(20),
                  server_default='file', nullable=False,
                  comment='Source category: file, url'))
    op.add_column('knowledge_sources',
        sa.Column('source_url', sa.String(2048), nullable=True,
                  comment='Original URL for url-type sources'))
    op.add_column('knowledge_sources',
        sa.Column('file_size_bytes', sa.Integer(), nullable=True,
                  comment='File/content size in bytes (for storage tracking)'))

    # Create index for source_category
    op.create_index('ix_knowledge_sources_source_category',
                    'knowledge_sources', ['source_category'])


def downgrade() -> None:
    """Remove subscription tracking and knowledge source limits."""

    # Drop in reverse order

    # 4. Drop knowledge_sources fields
    op.drop_index('ix_knowledge_sources_source_category', table_name='knowledge_sources')
    op.drop_column('knowledge_sources', 'file_size_bytes')
    op.drop_column('knowledge_sources', 'source_url')
    op.drop_column('knowledge_sources', 'source_category')

    # 3. Drop tenant_capabilities fields
    op.drop_column('tenant_capabilities', 'max_team_members')
    op.drop_column('tenant_capabilities', 'can_use_url_sources')
    op.drop_column('tenant_capabilities', 'max_knowledge_sources')

    # 2. Drop tenant subscription fields
    op.drop_index('ix_tenants_trial_ends_at', table_name='tenants')
    op.drop_index('ix_tenants_subscription_status', table_name='tenants')
    op.drop_index('ix_tenants_subscription_tier', table_name='tenants')
    op.drop_column('tenants', 'subscription_ends_at')
    op.drop_column('tenants', 'subscription_starts_at')
    op.drop_column('tenants', 'trial_ends_at')
    op.drop_column('tenants', 'billing_cycle')
    op.drop_column('tenants', 'subscription_status')
    op.drop_column('tenants', 'subscription_tier')
