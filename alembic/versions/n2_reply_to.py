"""N2 reply-to: messages.reply_to_message_id + encrypted quote snapshot

Revision ID: n2replyto
Revises: n4agentrating
Create Date: 2026-10-14

reply_to_message_id is a self-referencing nullable FK (ON DELETE SET NULL: a reply
outlives its original). reply_to_snapshot holds {sender_type, kind, snippet} frozen at
send time; it is app-side encrypted (EncryptedJSON) so it is plain TEXT at the DB level.
Existing rows stay NULL, i.e. "not a reply".
"""

import sqlalchemy as sa
from alembic import op

revision = "n2replyto"
down_revision = "n4agentrating"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("reply_to_message_id", sa.Uuid(), nullable=True))
    op.add_column("messages", sa.Column("reply_to_snapshot", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_messages_reply_to_message_id",
        "messages",
        "messages",
        ["reply_to_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_messages_reply_to_message_id", "messages", ["reply_to_message_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_reply_to_message_id", table_name="messages")
    op.drop_constraint("fk_messages_reply_to_message_id", "messages", type_="foreignkey")
    op.drop_column("messages", "reply_to_snapshot")
    op.drop_column("messages", "reply_to_message_id")
