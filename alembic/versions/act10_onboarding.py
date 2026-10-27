"""activation onboarding: widget-seen tracking on tenants, failure reason on knowledge sources

Revision ID: act10onboarding
Revises: wa10whatsapp
Create Date: 2026-10-28

* tenants.widget_first_seen_at / widget_last_seen_at / widget_last_seen_host: set when the public
  widget config is fetched from a customer site. Hostname only (no scheme, port, path or query).
* knowledge_sources.failure_code: why a FAILED source failed (document_too_short,
  no_text_extracted, embedding_failed, vector_store_unavailable); NULL for every other source.

All columns are nullable with no default, so nothing is rewritten and existing rows are untouched.
"""

import sqlalchemy as sa
from alembic import op

revision = "act10onboarding"
down_revision = "wa10whatsapp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tenants and knowledge_sources are read on every chat turn: fail fast, don't queue.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "tenants", sa.Column("widget_first_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tenants", sa.Column("widget_last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("tenants", sa.Column("widget_last_seen_host", sa.String(255), nullable=True))
    op.add_column("knowledge_sources", sa.Column("failure_code", sa.String(40), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("knowledge_sources", "failure_code")
    op.drop_column("tenants", "widget_last_seen_host")
    op.drop_column("tenants", "widget_last_seen_at")
    op.drop_column("tenants", "widget_first_seen_at")
