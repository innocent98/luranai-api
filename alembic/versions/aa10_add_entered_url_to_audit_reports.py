"""add entered_url to audit_reports

Revision ID: aa10enteredurl
Revises: b7d21c9f3a04
Create Date: 2026-07-25

"""
from alembic import op
import sqlalchemy as sa

revision = "aa10enteredurl"
down_revision = "b7d21c9f3a04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_reports",
        sa.Column("entered_url", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_reports", "entered_url")
