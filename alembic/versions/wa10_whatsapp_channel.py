"""whatsapp channel: connections, chats, inbound ledger, capability columns

Revision ID: wa10whatsapp
Revises: pf10planfeatures
Create Date: 2026-10-27

Adds the linked-device WhatsApp channel tables (tenant-scoped), the grant-only
capability columns (`can_use_whatsapp`, `max_whatsapp_numbers`) and the `whatsapp`
visitor source. `conversation_sessions.source` is a plain String(32), so the new
'whatsapp' source needs no DDL.

The pairing code is never stored: there is no column for it.
"""

import sqlalchemy as sa
from alembic import op

revision = "wa10whatsapp"
down_revision = "pf10planfeatures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD COLUMN on a table read every chat turn takes ACCESS EXCLUSIVE: fail fast, not queue.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "tenant_capabilities",
        sa.Column("can_use_whatsapp", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tenant_capabilities",
        sa.Column("max_whatsapp_numbers", sa.Integer(), nullable=False, server_default="0"),
    )

    # Postgres 15 permits ADD VALUE inside a transaction as long as the value is not
    # *used* in the same transaction (it is not here). visitor_source stores lowercase values.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE visitor_source ADD VALUE IF NOT EXISTS 'whatsapp'")

    op.create_table(
        "whatsapp_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("phone_e164", sa.Text(), nullable=False),
        sa.Column("phone_hmac", sa.String(64), nullable=False),
        sa.Column("session_key", sa.String(64), nullable=False),
        sa.Column("consent_version", sa.String(32), nullable=False),
        sa.Column("consent_accepted_by", sa.Uuid(), nullable=True),
        sa.Column("consent_accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_disconnect_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_disconnect_reason", sa.String(64), nullable=True),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["consent_accepted_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("phone_hmac", name="uq_whatsapp_connections_phone_hmac"),
        sa.UniqueConstraint("session_key", name="uq_whatsapp_connections_session_key"),
    )
    op.create_index("ix_whatsapp_connections_id", "whatsapp_connections", ["id"])
    op.create_index("ix_whatsapp_connections_tenant_id", "whatsapp_connections", ["tenant_id"])
    op.create_index(
        "ix_whatsapp_connections_tenant_status", "whatsapp_connections", ["tenant_id", "status"]
    )

    op.create_table(
        "whatsapp_chats",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("contact_hmac", sa.String(64), nullable=False),
        sa.Column("contact_ref", sa.Text(), nullable=False),
        sa.Column("visitor_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("human_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["whatsapp_connections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["visitor_id"], ["visitors.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("connection_id", "contact_hmac", name="uq_whatsapp_chat_contact"),
    )
    op.create_index("ix_whatsapp_chats_id", "whatsapp_chats", ["id"])
    op.create_index("ix_whatsapp_chats_tenant_id", "whatsapp_chats", ["tenant_id"])
    op.create_index("ix_whatsapp_chats_connection_id", "whatsapp_chats", ["connection_id"])
    op.create_index("ix_whatsapp_chats_session_id", "whatsapp_chats", ["session_id"])

    op.create_table(
        "whatsapp_inbound_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("provider_message_id", sa.String(128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["whatsapp_connections.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "connection_id", "provider_message_id", name="uq_whatsapp_inbound_provider_message"
        ),
    )
    op.create_index("ix_whatsapp_inbound_messages_id", "whatsapp_inbound_messages", ["id"])
    op.create_index(
        "ix_whatsapp_inbound_messages_connection_id", "whatsapp_inbound_messages", ["connection_id"]
    )


def downgrade() -> None:
    op.drop_table("whatsapp_inbound_messages")
    op.drop_table("whatsapp_chats")
    op.drop_table("whatsapp_connections")
    op.drop_column("tenant_capabilities", "max_whatsapp_numbers")
    op.drop_column("tenant_capabilities", "can_use_whatsapp")
    # Postgres cannot drop a single enum value: 'whatsapp' stays in visitor_source (harmless).
