"""encrypt User and PlatformAdmin PII + add email_hash columns

Revision ID: 3d8e2fa91c07
Revises: 2c7f9a1b4d8e
Create Date: 2026-04-14 12:00:00.000000

Extends the encryption-at-rest scope to tenant staff (User) and platform
administrators (PlatformAdmin). Follows the same pattern as CustomerUser:
- Change email/full_name columns to EncryptedString (Text-level no-op)
- Drop unique constraint and index on plaintext email
- Add nullable email_hash column with unique+active indexes
- Backfill populates email_hash; a follow-up migration tightens NOT NULL
"""

import app.core.encryption  # noqa: F401 — referenced by type calls below
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "3d8e2fa91c07"
down_revision = "2c7f9a1b4d8e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- users ----
    op.alter_column(
        "users",
        "email",
        existing_type=sa.VARCHAR(length=255),
        type_=app.core.encryption.EncryptedString(),
        existing_comment="User email address (unique across platform)",
        comment="User email address — encrypted at rest",
        existing_nullable=False,
    )
    op.alter_column(
        "users",
        "full_name",
        existing_type=sa.VARCHAR(length=255),
        type_=app.core.encryption.EncryptedString(),
        existing_comment="Full name of the user",
        comment="Full name of the user — encrypted at rest",
        existing_nullable=False,
    )
    op.add_column(
        "users",
        sa.Column(
            "email_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 hash of email for lookups",
        ),
    )
    # Drop plaintext email unique + indexes (defensive — names may vary across envs)
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key")
    op.execute("DROP INDEX IF EXISTS ix_users_email")
    op.execute("DROP INDEX IF EXISTS ix_users_email_active")
    # Create hash-based indexes
    op.create_index("ix_users_email_hash", "users", ["email_hash"], unique=True)
    op.create_index(
        "ix_users_email_hash_active", "users", ["email_hash", "is_active"], unique=False
    )

    # ---- platform_admins ----
    op.alter_column(
        "platform_admins",
        "email",
        existing_type=sa.VARCHAR(length=255),
        type_=app.core.encryption.EncryptedString(),
        existing_comment="Admin email address (unique across platform)",
        comment="Admin email address — encrypted at rest",
        existing_nullable=False,
    )
    op.alter_column(
        "platform_admins",
        "full_name",
        existing_type=sa.VARCHAR(length=255),
        type_=app.core.encryption.EncryptedString(),
        existing_comment="Full name of the admin",
        comment="Full name of the admin — encrypted at rest",
        existing_nullable=False,
    )
    op.add_column(
        "platform_admins",
        sa.Column(
            "email_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 hash of email for lookups",
        ),
    )
    op.execute(
        "ALTER TABLE platform_admins DROP CONSTRAINT IF EXISTS platform_admins_email_key"
    )
    op.execute("DROP INDEX IF EXISTS ix_platform_admins_email")
    op.create_index(
        "ix_platform_admins_email_hash", "platform_admins", ["email_hash"], unique=True
    )


def downgrade() -> None:
    # ---- platform_admins ----
    op.drop_index("ix_platform_admins_email_hash", table_name="platform_admins")
    op.create_index("ix_platform_admins_email", "platform_admins", ["email"], unique=True)
    op.create_unique_constraint(
        "platform_admins_email_key", "platform_admins", ["email"]
    )
    op.drop_column("platform_admins", "email_hash")
    op.alter_column(
        "platform_admins",
        "full_name",
        existing_type=app.core.encryption.EncryptedString(),
        type_=sa.VARCHAR(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "platform_admins",
        "email",
        existing_type=app.core.encryption.EncryptedString(),
        type_=sa.VARCHAR(length=255),
        existing_nullable=False,
    )

    # ---- users ----
    op.drop_index("ix_users_email_hash_active", table_name="users")
    op.drop_index("ix_users_email_hash", table_name="users")
    op.create_index("ix_users_email_active", "users", ["email", "is_active"], unique=False)
    op.create_index("ix_users_email", "users", ["email"], unique=False)
    op.create_unique_constraint("users_email_key", "users", ["email"])
    op.drop_column("users", "email_hash")
    op.alter_column(
        "users",
        "full_name",
        existing_type=app.core.encryption.EncryptedString(),
        type_=sa.VARCHAR(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "users",
        "email",
        existing_type=app.core.encryption.EncryptedString(),
        type_=sa.VARCHAR(length=255),
        existing_nullable=False,
    )
