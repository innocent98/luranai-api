"""whatsapp chats: remember the open rating request (1-5 reply handling)

Revision ID: wa20ratingreply
Revises: ent10entitlements
Create Date: 2026-10-28

A rating request prompt delivered to a WhatsApp chat is remembered on the chat row so a bare
"1".."5" reply can be recorded against it. Only the request id and its expiry are stored:
never the rating token.
"""

import sqlalchemy as sa
from alembic import op

revision = "wa20ratingreply"
down_revision = "ent10entitlements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # whatsapp_chats is read on every inbound message: fail fast instead of queueing behind it.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("whatsapp_chats", sa.Column("rating_request_id", sa.Uuid(), nullable=True))
    op.add_column(
        "whatsapp_chats",
        sa.Column("rating_request_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_whatsapp_chats_rating_request_id",
        "whatsapp_chats",
        "rating_requests",
        ["rating_request_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("fk_whatsapp_chats_rating_request_id", "whatsapp_chats", type_="foreignkey")
    op.drop_column("whatsapp_chats", "rating_request_expires_at")
    op.drop_column("whatsapp_chats", "rating_request_id")
