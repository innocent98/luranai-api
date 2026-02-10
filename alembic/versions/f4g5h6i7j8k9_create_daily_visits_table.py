"""create daily visits table

Revision ID: f4g5h6i7j8k9
Revises: e3f4g5h6i7j8
Create Date: 2026-02-10 03:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4g5h6i7j8k9"
down_revision: str | None = "e3f4g5h6i7j8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create daily visit aggregation table."""
    op.create_table(
        "daily_visits",
        sa.Column("visit_date", sa.Date(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("visitor_key", sa.String(length=128), nullable=False),
        sa.Column("visit_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "visit_date",
            "tenant_id",
            "source",
            "visitor_key",
            name="uq_daily_visits_per_visitor_day",
        ),
    )
    op.create_index("ix_daily_visits_date_source", "daily_visits", ["visit_date", "source"], unique=False)
    op.create_index("ix_daily_visits_tenant_date", "daily_visits", ["tenant_id", "visit_date"], unique=False)
    op.create_index(op.f("ix_daily_visits_id"), "daily_visits", ["id"], unique=False)
    op.create_index(op.f("ix_daily_visits_source"), "daily_visits", ["source"], unique=False)
    op.create_index(op.f("ix_daily_visits_tenant_id"), "daily_visits", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_daily_visits_visit_date"), "daily_visits", ["visit_date"], unique=False)
    op.create_index(op.f("ix_daily_visits_visitor_key"), "daily_visits", ["visitor_key"], unique=False)


def downgrade() -> None:
    """Drop daily visit aggregation table."""
    op.drop_index(op.f("ix_daily_visits_visitor_key"), table_name="daily_visits")
    op.drop_index(op.f("ix_daily_visits_visit_date"), table_name="daily_visits")
    op.drop_index(op.f("ix_daily_visits_tenant_id"), table_name="daily_visits")
    op.drop_index(op.f("ix_daily_visits_source"), table_name="daily_visits")
    op.drop_index(op.f("ix_daily_visits_id"), table_name="daily_visits")
    op.drop_index("ix_daily_visits_tenant_date", table_name="daily_visits")
    op.drop_index("ix_daily_visits_date_source", table_name="daily_visits")
    op.drop_table("daily_visits")
