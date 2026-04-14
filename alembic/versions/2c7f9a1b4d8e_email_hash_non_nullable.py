"""make customer_users.email_hash non-nullable after PII backfill

Revision ID: 2c7f9a1b4d8e
Revises: 145bebe88ecb
Create Date: 2026-04-14 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2c7f9a1b4d8e'
down_revision = '145bebe88ecb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Safety: fail early if any row is still NULL (backfill not run / incomplete).
    conn = op.get_bind()
    null_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM customer_users WHERE email_hash IS NULL")
    ).scalar()
    if null_count and null_count > 0:
        raise RuntimeError(
            f"Cannot set email_hash NOT NULL: {null_count} row(s) still have NULL. "
            "Run scripts/backfill_encrypt_pii.py first."
        )

    op.alter_column(
        'customer_users',
        'email_hash',
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        'customer_users',
        'email_hash',
        existing_type=sa.String(length=64),
        nullable=True,
    )
