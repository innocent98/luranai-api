"""add public slug and visitor identity models

Revision ID: o3p4q5r6s7t8
Revises: n2o3p4q5r6s7
Create Date: 2026-03-21 12:00:00.000000
"""

import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "o3p4q5r6s7t8"
down_revision = "n2o3p4q5r6s7"
branch_labels = None
depends_on = None


visitor_source_enum = postgresql.ENUM(
    "widget",
    "support_page",
    name="visitor_source",
    create_type=False,
)
visitor_session_entry_enum = postgresql.ENUM(
    "widget",
    "support_page",
    name="visitor_session_entry_point",
    create_type=False,
)
visitor_device_type_enum = postgresql.ENUM(
    "desktop",
    "mobile",
    "tablet",
    name="visitor_device_type",
    create_type=False,
)


def _normalize_slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized[:50].strip("-") or "support"


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("tenants", sa.Column("public_slug", sa.String(length=50), nullable=True))
    op.create_index(op.f("ix_tenants_public_slug"), "tenants", ["public_slug"], unique=True)

    tenants = list(bind.execute(sa.text("SELECT id, display_name, name FROM tenants")))
    used_slugs: set[str] = set()
    for tenant_id, display_name, name in tenants:
        base_slug = _normalize_slug(name or display_name or "support")
        candidate = base_slug
        suffix = 2
        while candidate in used_slugs:
            suffix_text = f"-{suffix}"
            trimmed = base_slug[: 50 - len(suffix_text)].rstrip("-")
            candidate = f"{trimmed}{suffix_text}"
            suffix += 1
        used_slugs.add(candidate)
        bind.execute(
            sa.text("UPDATE tenants SET public_slug = :slug WHERE id = :tenant_id"),
            {"slug": candidate, "tenant_id": tenant_id},
        )

    visitor_source_enum.create(bind, checkfirst=True)
    visitor_session_entry_enum.create(bind, checkfirst=True)
    visitor_device_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "visitors",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("public_token", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_from", visitor_source_enum, nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_visitors_id"), "visitors", ["id"], unique=False)
    op.create_index(op.f("ix_visitors_last_seen_at"), "visitors", ["last_seen_at"], unique=False)
    op.create_index(op.f("ix_visitors_public_token"), "visitors", ["public_token"], unique=False)
    op.create_index(op.f("ix_visitors_tenant_id"), "visitors", ["tenant_id"], unique=False)
    op.create_index("ix_visitors_tenant_token", "visitors", ["tenant_id", "public_token"], unique=True)
    op.create_index(
        "ix_visitors_tenant_last_seen",
        "visitors",
        ["tenant_id", "last_seen_at"],
        unique=False,
    )

    op.create_table(
        "visitor_sessions",
        sa.Column("visitor_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_token", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_point", visitor_session_entry_enum, nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("device_type", visitor_device_type_enum, nullable=True),
        sa.Column("browser_name", sa.String(length=100), nullable=True),
        sa.Column("os_name", sa.String(length=100), nullable=True),
        sa.Column("referrer_url", sa.Text(), nullable=True),
        sa.Column("landing_url", sa.Text(), nullable=True),
        sa.Column("locale", sa.String(length=32), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["visitor_id"], ["visitors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_visitor_sessions_id"), "visitor_sessions", ["id"], unique=False)
    op.create_index(
        op.f("ix_visitor_sessions_last_activity_at"),
        "visitor_sessions",
        ["last_activity_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_visitor_sessions_tenant_id"),
        "visitor_sessions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_visitor_sessions_visitor_id"),
        "visitor_sessions",
        ["visitor_id"],
        unique=False,
    )
    op.create_index(
        "ix_visitor_sessions_visitor_started",
        "visitor_sessions",
        ["visitor_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_visitor_sessions_tenant_last_activity",
        "visitor_sessions",
        ["tenant_id", "last_activity_at"],
        unique=False,
    )
    op.create_index("ix_visitor_sessions_token", "visitor_sessions", ["session_token"], unique=True)

    op.add_column("conversation_sessions", sa.Column("visitor_id", sa.Uuid(), nullable=True))
    op.add_column("conversation_sessions", sa.Column("visitor_session_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_conversation_sessions_visitor_id"),
        "conversation_sessions",
        ["visitor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_sessions_visitor_session_id"),
        "conversation_sessions",
        ["visitor_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_sessions_visitor",
        "conversation_sessions",
        ["visitor_id", "last_message_at"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_conversation_sessions_visitor_id",
        "conversation_sessions",
        "visitors",
        ["visitor_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_conversation_sessions_visitor_session_id",
        "conversation_sessions",
        "visitor_sessions",
        ["visitor_session_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_conversation_sessions_visitor_session_id",
        "conversation_sessions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_conversation_sessions_visitor_id",
        "conversation_sessions",
        type_="foreignkey",
    )
    op.drop_index("ix_conversation_sessions_visitor", table_name="conversation_sessions")
    op.drop_index(op.f("ix_conversation_sessions_visitor_session_id"), table_name="conversation_sessions")
    op.drop_index(op.f("ix_conversation_sessions_visitor_id"), table_name="conversation_sessions")
    op.drop_column("conversation_sessions", "visitor_session_id")
    op.drop_column("conversation_sessions", "visitor_id")

    op.drop_index("ix_visitor_sessions_token", table_name="visitor_sessions")
    op.drop_index("ix_visitor_sessions_tenant_last_activity", table_name="visitor_sessions")
    op.drop_index("ix_visitor_sessions_visitor_started", table_name="visitor_sessions")
    op.drop_index(op.f("ix_visitor_sessions_visitor_id"), table_name="visitor_sessions")
    op.drop_index(op.f("ix_visitor_sessions_tenant_id"), table_name="visitor_sessions")
    op.drop_index(op.f("ix_visitor_sessions_last_activity_at"), table_name="visitor_sessions")
    op.drop_index(op.f("ix_visitor_sessions_id"), table_name="visitor_sessions")
    op.drop_table("visitor_sessions")

    op.drop_index("ix_visitors_tenant_last_seen", table_name="visitors")
    op.drop_index("ix_visitors_tenant_token", table_name="visitors")
    op.drop_index(op.f("ix_visitors_tenant_id"), table_name="visitors")
    op.drop_index(op.f("ix_visitors_public_token"), table_name="visitors")
    op.drop_index(op.f("ix_visitors_last_seen_at"), table_name="visitors")
    op.drop_index(op.f("ix_visitors_id"), table_name="visitors")
    op.drop_table("visitors")

    visitor_device_type_enum.drop(op.get_bind(), checkfirst=True)
    visitor_session_entry_enum.drop(op.get_bind(), checkfirst=True)
    visitor_source_enum.drop(op.get_bind(), checkfirst=True)

    op.drop_index(op.f("ix_tenants_public_slug"), table_name="tenants")
    op.drop_column("tenants", "public_slug")
