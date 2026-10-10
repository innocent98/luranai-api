"""message_attachments.position (send order)

Revision ID: mp70attachpos
Revises: mt60missedrepair
Create Date: 2026-10-10

Attachments were returned ordered by upload `created_at`; the composer uploads
concurrently, so a message sent as [img1, img2] could reload as [img2, img1].
`position` records the index in the request's attachment_ids at link time.
Existing rows are backfilled per message in created_at order (the best order
that is still recoverable).
"""

import sqlalchemy as sa
from alembic import op

revision = "mp70attachpos"
down_revision = "mt60missedrepair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_attachments",
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Send order within the message (index in the request's attachment_ids)",
        ),
    )
    op.execute(
        """
        UPDATE message_attachments AS m
        SET position = r.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY message_id ORDER BY created_at, id
            ) - 1 AS rn
            FROM message_attachments
            WHERE message_id IS NOT NULL
        ) AS r
        WHERE m.id = r.id
        """
    )


def downgrade() -> None:
    op.drop_column("message_attachments", "position")
