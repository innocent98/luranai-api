"""add message_attachments table

Revision ID: ma50messageattach
Revises: ad40accountdeletion
Create Date: 2026-10-07

Task 3 of the chat-image-upload backend. `url` is app-side encrypted
(EncryptedString TypeDecorator) so it is plain TEXT at the DB level.
Enum labels are the Python member NAMES (uppercase), matching SQLAlchemy's
default Enum behaviour used by the other conversation enums.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

# revision identifiers, used by Alembic.
revision = "ma50messageattach"
down_revision = "ad40accountdeletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    direction = ENUM("INBOUND", "OUTBOUND", name="attachment_direction", create_type=False)
    status = ENUM("UPLOADED", "LINKED", name="attachment_status", create_type=False)
    moderation = ENUM("PASSED", name="attachment_moderation", create_type=False)
    direction.create(bind, checkfirst=True)
    status.create(bind, checkfirst=True)
    moderation.create(bind, checkfirst=True)

    op.create_table(
        "message_attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("direction", direction, nullable=False),
        sa.Column("cloudinary_public_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("moderation_status", moderation, nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_attachments_message_id", "message_attachments", ["message_id"])
    op.create_index("ix_message_attachments_session_id", "message_attachments", ["session_id"])
    op.create_index("ix_message_attachments_tenant_id", "message_attachments", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_message_attachments_tenant_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_session_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_message_id", table_name="message_attachments")
    op.drop_table("message_attachments")
    bind = op.get_bind()
    ENUM(name="attachment_moderation").drop(bind, checkfirst=True)
    ENUM(name="attachment_status").drop(bind, checkfirst=True)
    ENUM(name="attachment_direction").drop(bind, checkfirst=True)
