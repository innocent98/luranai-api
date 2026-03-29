"""add require_name_before_escalation flag

Revision ID: p4q5r6s7t8u9
Revises: o3p4q5r6s7t8
Create Date: 2026-03-21 13:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "p4q5r6s7t8u9"
down_revision = "o3p4q5r6s7t8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "require_name_before_escalation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "tenant_ai_configs",
        "require_name_before_escalation",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("tenant_ai_configs", "require_name_before_escalation")
