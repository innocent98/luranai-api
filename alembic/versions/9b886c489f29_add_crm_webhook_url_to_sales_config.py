"""add crm_webhook_url to sales config

Revision ID: 9b886c489f29
Revises: 6a109f12a6b0
Create Date: 2026-06-30 14:10:32.130697

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9b886c489f29'
down_revision = '6a109f12a6b0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_sales_agent_config",
                  sa.Column("crm_webhook_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_sales_agent_config", "crm_webhook_url")
