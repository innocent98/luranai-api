"""add hosted support profile fields

Revision ID: r6s7t8u9v0w1
Revises: q5r6s7t8u9v0
Create Date: 2026-03-22 11:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "r6s7t8u9v0w1"
down_revision = "q5r6s7t8u9v0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("long_description", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("support_headline", sa.String(length=160), nullable=True))
    op.add_column("tenants", sa.Column("support_subheadline", sa.String(length=255), nullable=True))
    op.add_column("tenants", sa.Column("support_hours", sa.String(length=120), nullable=True))
    op.add_column("tenants", sa.Column("support_response_time", sa.String(length=120), nullable=True))
    op.add_column("tenants", sa.Column("support_footer_note", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "support_footer_note")
    op.drop_column("tenants", "support_response_time")
    op.drop_column("tenants", "support_hours")
    op.drop_column("tenants", "support_subheadline")
    op.drop_column("tenants", "support_headline")
    op.drop_column("tenants", "long_description")
