"""add pre-chat identity flags

Revision ID: q5r6s7t8u9v0
Revises: p4q5r6s7t8u9
Create Date: 2026-03-21 15:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "q5r6s7t8u9v0"
down_revision = "p4q5r6s7t8u9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_ai_configs",
        sa.Column("collect_name_before_chat", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tenant_ai_configs",
        sa.Column("collect_email_before_chat", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("tenant_ai_configs", "collect_name_before_chat", server_default=None)
    op.alter_column("tenant_ai_configs", "collect_email_before_chat", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_ai_configs", "collect_email_before_chat")
    op.drop_column("tenant_ai_configs", "collect_name_before_chat")
