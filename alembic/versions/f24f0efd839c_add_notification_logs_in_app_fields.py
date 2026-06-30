"""add notification_logs in-app fields

Revision ID: f24f0efd839c
Revises: d76946bef467
Create Date: 2026-06-29 23:11:04.756692

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f24f0efd839c'
down_revision = 'd76946bef467'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_logs", sa.Column("title", sa.String(length=160), nullable=True))
    op.add_column("notification_logs", sa.Column("body", sa.String(length=400), nullable=True))
    op.add_column("notification_logs", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("notification_logs", "read_at")
    op.drop_column("notification_logs", "body")
    op.drop_column("notification_logs", "title")
