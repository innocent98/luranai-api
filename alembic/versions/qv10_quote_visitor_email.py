"""quote emailed to the visitor: new notification event

Revision ID: qv10quotevisitoremail
Revises: wa40contactexclusion
Create Date: 2026-10-31

Adds `quote_emailed_visitor` to the `notif_event` enum so the visitor copy of a quote is logged,
deduplicated and counted (per address, per tenant) in `notification_logs` like every other
notification. No table or column changes.
"""

from alembic import op

revision = "qv10quotevisitoremail"
down_revision = "wa40contactexclusion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres 15 permits ADD VALUE inside a transaction as long as the value is not used in the
    # same transaction (it is not here). SQLite stores the enum as a plain string: nothing to do.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE notif_event ADD VALUE IF NOT EXISTS 'quote_emailed_visitor'")


def downgrade() -> None:
    # Postgres cannot drop one enum value. Leaving it is harmless: nothing reads it once the code
    # that writes it is gone. (Delete rows with this event first if the type must be rebuilt.)
    pass
