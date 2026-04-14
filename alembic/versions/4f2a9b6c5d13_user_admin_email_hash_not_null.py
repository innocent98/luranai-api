"""make users.email_hash and platform_admins.email_hash non-nullable

Revision ID: 4f2a9b6c5d13
Revises: 3d8e2fa91c07
Create Date: 2026-04-14 12:15:00.000000

Runs after backfill_encrypt_user_and_admin_pii populates email_hash for
all existing rows. Safety check aborts if any NULLs remain.
"""

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "4f2a9b6c5d13"
down_revision = "3d8e2fa91c07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    users_null = conn.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE email_hash IS NULL")
    ).scalar()
    admins_null = conn.execute(
        sa.text("SELECT COUNT(*) FROM platform_admins WHERE email_hash IS NULL")
    ).scalar()

    if (users_null or 0) > 0 or (admins_null or 0) > 0:
        raise RuntimeError(
            f"Cannot set email_hash NOT NULL: users={users_null} NULL, "
            f"platform_admins={admins_null} NULL. "
            "Run scripts/backfill_encrypt_user_admin.py first."
        )

    op.alter_column(
        "users",
        "email_hash",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.alter_column(
        "platform_admins",
        "email_hash",
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "platform_admins",
        "email_hash",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.alter_column(
        "users",
        "email_hash",
        existing_type=sa.String(length=64),
        nullable=True,
    )
