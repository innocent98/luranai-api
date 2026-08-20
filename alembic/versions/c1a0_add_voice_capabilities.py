"""add voice capabilities to tenant_capabilities

Revision ID: c1a0
Revises: ab20blockedquality
"""

import sqlalchemy as sa
from alembic import op

revision = "c1a0"
down_revision = "ab20blockedquality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_capabilities",
        sa.Column(
            "can_use_voice",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "tenant_capabilities",
        sa.Column("max_voice_minutes_per_month", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "max_voice_minutes_per_month")
    op.drop_column("tenant_capabilities", "can_use_voice")
