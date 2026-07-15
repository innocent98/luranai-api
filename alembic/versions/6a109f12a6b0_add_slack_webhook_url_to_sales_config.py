"""add slack_webhook_url to sales config

Revision ID: 6a109f12a6b0
Revises: f24f0efd839c
Create Date: 2026-06-30 00:21:32.078860

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6a109f12a6b0'
down_revision = 'f24f0efd839c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_sales_agent_config",
                  sa.Column("slack_webhook_url", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_sales_agent_config", "slack_webhook_url")
