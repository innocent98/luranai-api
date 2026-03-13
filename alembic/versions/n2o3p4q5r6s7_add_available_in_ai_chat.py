"""add available_in_ai_chat to tenant actions

Revision ID: n2o3p4q5r6s7
Revises: m1n2o3p4q5r6
Create Date: 2026-03-13 23:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "n2o3p4q5r6s7"
down_revision = "m1n2o3p4q5r6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_actions",
        sa.Column("available_in_ai_chat", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("tenant_actions", "available_in_ai_chat", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_actions", "available_in_ai_chat")
