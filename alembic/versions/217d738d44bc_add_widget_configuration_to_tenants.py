"""Add widget configuration to tenants

Revision ID: 217d738d44bc
Revises: 8f07aeb3b20c
Create Date: 2026-01-15 13:38:38.554213

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '217d738d44bc'
down_revision = '8f07aeb3b20c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add widget configuration columns to tenants table."""
    # Add widget configuration columns
    op.add_column('tenants', sa.Column('widget_primary_color', sa.String(length=7), nullable=True, comment='Primary color for widget (hex format)'))
    op.add_column('tenants', sa.Column('widget_accent_color', sa.String(length=7), nullable=True, comment='Accent color for widget (hex format)'))
    op.add_column('tenants', sa.Column('widget_logo_url', sa.String(length=500), nullable=True, comment='URL to company logo for widget'))
    op.add_column('tenants', sa.Column('widget_greeting_message', sa.String(length=500), nullable=True, comment='Greeting message shown in widget'))
    op.add_column('tenants', sa.Column('widget_position', sa.String(length=20), nullable=True, comment='Widget position: bottom-right, bottom-left, top-right, top-left'))
    op.add_column('tenants', sa.Column('widget_show_unread_badge', sa.Boolean(), nullable=False, server_default='true', comment='Show unread message badge on widget button'))
    op.add_column('tenants', sa.Column('widget_allow_file_upload', sa.Boolean(), nullable=False, server_default='true', comment='Allow file uploads in widget chat'))


def downgrade() -> None:
    """Remove widget configuration columns from tenants table."""
    op.drop_column('tenants', 'widget_allow_file_upload')
    op.drop_column('tenants', 'widget_show_unread_badge')
    op.drop_column('tenants', 'widget_position')
    op.drop_column('tenants', 'widget_greeting_message')
    op.drop_column('tenants', 'widget_logo_url')
    op.drop_column('tenants', 'widget_accent_color')
    op.drop_column('tenants', 'widget_primary_color')
