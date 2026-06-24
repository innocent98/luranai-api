"""create_sales_agent_config

Revision ID: 145010cdeb0e
Revises: f029102ebfa3
Create Date: 2026-06-24 13:43:26.272886

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '145010cdeb0e'
down_revision = 'f029102ebfa3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'tenant_sales_agent_config',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False, comment='One config per tenant'),
        sa.Column('agent_enabled', sa.Boolean(), nullable=False, comment='Master switch — enables the AI sales agent for this tenant'),
        sa.Column('lead_capture_enabled', sa.Boolean(), nullable=False, comment='Capture leads when sales intents are detected'),
        sa.Column('meeting_booking_enabled', sa.Boolean(), nullable=False, comment='Allow the agent to schedule meetings via calendar_provider'),
        sa.Column('quotation_enabled', sa.Boolean(), nullable=False, comment='Allow the agent to generate pricing quotations'),
        sa.Column('crm_provider', sa.String(length=80), nullable=True, comment="CRM provider slug (e.g. 'hubspot', 'pipedrive')"),
        sa.Column('calendar_provider', sa.String(length=80), nullable=True, comment="Calendar provider slug (e.g. 'google', 'calendly')"),
        sa.Column('notify_channels', sa.JSON(), nullable=False, comment='Notification channels: {email, slack, in_app}'),
        sa.Column('notification_email', sa.String(length=255), nullable=True, comment='Email address for sales notifications (overrides tenant owner email)'),
        sa.Column('trigger_intents', sa.JSON(), nullable=False, comment='Intent strings that activate sales capture'),
        sa.Column('qualification_questions', sa.JSON(), nullable=False, comment='Ordered list of qualification question dicts'),
        sa.Column('pricing_tiers', sa.JSON(), nullable=False, comment='Tenant-defined pricing tier dicts for quotation generation'),
        sa.Column('currency', sa.String(length=10), nullable=False, comment='ISO 4217 currency code used in quotations (default NGN)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_tenant_sales_agent_config_id'),
        'tenant_sales_agent_config',
        ['id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_tenant_sales_agent_config_tenant_id'),
        'tenant_sales_agent_config',
        ['tenant_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_tenant_sales_agent_config_tenant_id'), table_name='tenant_sales_agent_config')
    op.drop_index(op.f('ix_tenant_sales_agent_config_id'), table_name='tenant_sales_agent_config')
    op.drop_table('tenant_sales_agent_config')
