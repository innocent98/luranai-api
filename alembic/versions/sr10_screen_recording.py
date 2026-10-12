"""screen recording: attachment kind/video columns, retention, gate flags

Revision ID: sr10screenrec
Revises: n3firstresp
Create Date: 2026-10-12

Adds kind (IMAGE|VIDEO), duration_ms, poster_url, source_format, expires_at to
message_attachments; url becomes nullable (NULL once expired); EXPIRED status and
NOT_APPLICABLE moderation values; partial unique index on video public ids;
tenant gate columns. Existing rows get expires_at = migration time + 30 d
(SR-D6: 30-day grace from rollout). Enum values are stored as NAMES.
"""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

revision = "sr10screenrec"
down_revision = "n3firstresp"
branch_labels = None
depends_on = None

BACKFILL_SQL = "UPDATE message_attachments SET expires_at = :expires_at WHERE expires_at IS NULL"


def backfill_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=30)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Additive enum values; PG15 allows ADD VALUE in a txn when unused in that txn.
    op.execute("ALTER TYPE attachment_status ADD VALUE IF NOT EXISTS 'EXPIRED'")
    op.execute("ALTER TYPE attachment_moderation ADD VALUE IF NOT EXISTS 'NOT_APPLICABLE'")
    kind = ENUM("IMAGE", "VIDEO", name="attachment_kind", create_type=False)
    kind.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "message_attachments",
        sa.Column("kind", kind, nullable=False, server_default="IMAGE"),
    )
    op.add_column("message_attachments", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column("message_attachments", sa.Column("poster_url", sa.Text(), nullable=True))
    op.add_column(
        "message_attachments", sa.Column("source_format", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "message_attachments",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.get_bind().execute(sa.text(BACKFILL_SQL), {"expires_at": backfill_expiry()})
    op.alter_column(
        "message_attachments",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now() + interval '30 days'"),
    )
    op.alter_column("message_attachments", "url", existing_type=sa.Text(), nullable=True)
    op.create_index("ix_message_attachments_expires_at", "message_attachments", ["expires_at"])
    op.create_index(
        "uq_message_attachments_video_public_id",
        "message_attachments",
        ["cloudinary_public_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'VIDEO'"),
    )

    op.add_column(
        "tenant_capabilities",
        sa.Column(
            "can_use_screen_recording", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "widget_allow_screen_recording",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    # Video rows and expired rows cannot be represented pre-sr10. Their Cloudinary
    # assets are NOT destroyed here: run sweep_unregistered_videos / purge first if needed.
    op.execute("DELETE FROM message_attachments WHERE kind = 'VIDEO' OR url IS NULL")
    op.drop_column("tenants", "widget_allow_screen_recording")
    op.drop_column("tenant_capabilities", "can_use_screen_recording")
    op.drop_index("uq_message_attachments_video_public_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_expires_at", table_name="message_attachments")
    op.alter_column("message_attachments", "url", existing_type=sa.Text(), nullable=False)
    op.drop_column("message_attachments", "expires_at")
    op.drop_column("message_attachments", "source_format")
    op.drop_column("message_attachments", "poster_url")
    op.drop_column("message_attachments", "duration_ms")
    op.drop_column("message_attachments", "kind")
    ENUM(name="attachment_kind").drop(op.get_bind(), checkfirst=True)
    # EXPIRED / NOT_APPLICABLE enum values stay (Postgres cannot drop enum values).
