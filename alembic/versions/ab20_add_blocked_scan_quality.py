"""add blocked to audit_scan_quality enum

Revision ID: ab20blockedquality
Revises: aa10enteredurl
Create Date: 2026-07-28
"""
from alembic import op

revision = "ab20blockedquality"
down_revision = "aa10enteredurl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive enum value. Postgres 15 permits ADD VALUE inside a transaction as
    # long as the value is not used in the same transaction (it isn't here).
    op.execute("ALTER TYPE audit_scan_quality ADD VALUE IF NOT EXISTS 'blocked'")


def downgrade() -> None:
    # Postgres cannot drop an enum value without recreating the type; the extra
    # value is inert when unused, so downgrade is intentionally a no-op.
    pass
