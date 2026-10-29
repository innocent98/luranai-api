"""WhatsApp contact exclusion list (per connection)

Revision ID: wa40contactexclusion
Revises: cl10conversationlimits
Create Date: 2026-10-29

Adds `whatsapp_contact_exclusions`: contacts of a connected number that Luran must ignore
entirely (the tenant's personal contacts). Holds only peppered HMAC keys (`phone_hmac`, and a
learned `lid_hmac`) plus an encrypted display mask and optional encrypted label. The full
number is never stored.

Also adds `whatsapp_chats.phone_hmac` (nullable, peppered HMAC, set once the sidecar resolved the
contact's digits) so a contact who is excluded AFTER they already have a LID-keyed chat can still
be recognised without waiting for another event.

Additive and reversible: a new table with FKs to tenants and whatsapp_connections (ON DELETE
CASCADE); the downgrade drops it. No existing table is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "wa40contactexclusion"
down_revision = "cl10conversationlimits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable column on a small table; fail fast rather than queue behind a long lock.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("whatsapp_chats", sa.Column("phone_hmac", sa.String(64), nullable=True))
    op.create_index(
        "ix_whatsapp_chats_connection_phone", "whatsapp_chats", ["connection_id", "phone_hmac"]
    )
    op.create_table(
        "whatsapp_contact_exclusions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("phone_hmac", sa.String(64), nullable=False),
        sa.Column("lid_hmac", sa.String(64), nullable=True),
        sa.Column("phone_masked", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["whatsapp_connections.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("connection_id", "phone_hmac", name="uq_whatsapp_exclusion_phone"),
    )
    op.create_index(
        "ix_whatsapp_contact_exclusions_id", "whatsapp_contact_exclusions", ["id"], unique=False
    )
    op.create_index(
        "ix_whatsapp_contact_exclusions_tenant_id",
        "whatsapp_contact_exclusions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_whatsapp_contact_exclusions_connection_id",
        "whatsapp_contact_exclusions",
        ["connection_id"],
    )
    op.create_index(
        "ix_whatsapp_exclusions_connection_lid",
        "whatsapp_contact_exclusions",
        ["connection_id", "lid_hmac"],
    )


def downgrade() -> None:
    op.drop_table("whatsapp_contact_exclusions")  # drops its indexes with it
    op.drop_index("ix_whatsapp_chats_connection_phone", table_name="whatsapp_chats")
    op.drop_column("whatsapp_chats", "phone_hmac")
