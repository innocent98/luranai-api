"""ticket_number_counters: race-free per-year ticket numbering

Revision ID: t1ticketnumseq
Revises: n4agentrating
Create Date: 2026-10-14

Replaces the COUNT(*)+1 ticket-number generator with a per-year counter row that is
incremented atomically. Seeded from the highest existing TKT-YYYY-NNNN suffix per year
(MAX, not COUNT, so gaps from deleted tickets never cause a reuse). Tickets whose
number does not match the format are ignored.
"""

import sqlalchemy as sa

from alembic import op

revision = "t1ticketnumseq"
down_revision = "n4agentrating"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_number_counters",
        sa.Column("year", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("year"),
    )
    op.execute(
        r"""
        INSERT INTO ticket_number_counters (year, last_value)
        SELECT parts[1]::int, MAX(parts[2]::int)
        FROM (
            SELECT regexp_match(ticket_number, '^TKT-(\d{4})-(\d+)$') AS parts
            FROM tickets
        ) AS parsed
        WHERE parts IS NOT NULL
        GROUP BY parts[1]::int
        """
    )


def downgrade() -> None:
    op.drop_table("ticket_number_counters")
