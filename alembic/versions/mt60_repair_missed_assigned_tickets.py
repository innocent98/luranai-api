"""repair tickets stuck MISSED while assigned

Revision ID: mt60missedrepair
Revises: ma50messageattach
Create Date: 2026-10-10

Data migration. Before this release `TicketService.assign_ticket` only moved
OPEN -> IN_PROGRESS, so a MISSED ticket taken over from the dashboard kept
status MISSED with `assigned_to` set (prod TKT-2026-0007). Move those rows to
IN_PROGRESS.

ticket_status stores the Python member NAMES (uppercase; see ac30agentcaller),
so the literals below are 'MISSED' / 'IN_PROGRESS'. Idempotent: the WHERE clause
no longer matches once rows are repaired.

`status::text` (not a bare 'MISSED' literal) because on a fresh database
ac30agentcaller adds the MISSED enum value in the same transaction, and
Postgres forbids *using* a just-added enum value before commit.

Downgrade is a deliberate no-op: the original MISSED+assigned state was a bug,
and the affected rows cannot be told apart from legitimately in-progress ones.
"""

from alembic import op

revision = "mt60missedrepair"
down_revision = "ma50messageattach"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE tickets SET status = 'IN_PROGRESS' "
        "WHERE status::text = 'MISSED' AND assigned_to IS NOT NULL"
    )


def downgrade() -> None:
    pass
