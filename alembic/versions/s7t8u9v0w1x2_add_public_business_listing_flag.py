"""add public business listing flag

Revision ID: s7t8u9v0w1x2
Revises: r6s7t8u9v0w1
Create Date: 2026-03-22 12:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "s7t8u9v0w1x2"
down_revision = "r6s7t8u9v0w1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "is_publicly_listed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("tenants", "is_publicly_listed", server_default=None)


def downgrade() -> None:
    op.drop_column("tenants", "is_publicly_listed")
