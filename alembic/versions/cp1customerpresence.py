"""Customer presence: conversation_sessions.customer_last_seen_at

Revision ID: cp1customerpresence
Revises: n4agentrating
Create Date: 2026-10-14

Nullable timestamptz written when a session's customer goes offline (after the
30 s grace). Redis is the live source of truth; this column is the durable
"last seen" once the Redis keys have expired. Additive and nullable, so no
backfill and no lock-heavy rewrite.
"""

import sqlalchemy as sa
from alembic import op

revision = "cp1customerpresence"
down_revision = "n4agentrating"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column(
            "customer_last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the customer was last connected (written on offline transition)",
        ),
    )


def downgrade() -> None:
    op.drop_column("conversation_sessions", "customer_last_seen_at")
