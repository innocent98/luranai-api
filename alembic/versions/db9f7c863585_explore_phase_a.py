"""explore phase a

Revision ID: db9f7c863585
Revises: 4f2a9b6c5d13
Create Date: 2026-05-07 15:22:38.133076

Adds:
- tenants.category (String(40), nullable) — industry category for browse filter
- conversation_sessions.source (String(32), nullable) — attribution source
- conversation_sessions.source_meta (JSONB, nullable) — attribution context
- ix_tenants_listed_category — partial index on (is_publicly_listed, category)
- ix_conversation_sessions_source_created — composite index on (source, created_at)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'db9f7c863585'
down_revision = '4f2a9b6c5d13'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. tenants.category
    op.add_column(
        "tenants",
        sa.Column("category", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_tenants_listed_category",
        "tenants",
        ["is_publicly_listed", "category"],
        postgresql_where=sa.text("is_publicly_listed = true"),
    )

    # 2. conversation_sessions.source + source_meta
    op.add_column(
        "conversation_sessions",
        sa.Column("source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column(
            "source_meta",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_conversation_sessions_source_created",
        "conversation_sessions",
        ["source", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_sessions_source_created", table_name="conversation_sessions")
    op.drop_column("conversation_sessions", "source_meta")
    op.drop_column("conversation_sessions", "source")

    op.drop_index("ix_tenants_listed_category", table_name="tenants")
    op.drop_column("tenants", "category")
