"""agent caller: agent_devices, availability, ticket acknowledgement, push channel

Revision ID: ac30agentcaller
Revises: ab20blockedquality
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

revision = "ac30agentcaller"
down_revision = "ab20blockedquality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- additive enum values -------------------------------------------------
    # Postgres 15 permits ADD VALUE inside a transaction as long as the value is
    # not *used* in the same transaction (it is not here).
    # ticket_status is mapped without values_callable, so it stores member NAMES.
    op.execute("ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'MISSED'")
    # notif_channel / notif_event use values_callable, so they store lowercase values.
    op.execute("ALTER TYPE notif_channel ADD VALUE IF NOT EXISTS 'push'")
    op.execute("ALTER TYPE notif_event ADD VALUE IF NOT EXISTS 'escalation_alert'")
    op.execute("ALTER TYPE notif_event ADD VALUE IF NOT EXISTS 'escalation_fallback'")
    op.execute("ALTER TYPE notif_event ADD VALUE IF NOT EXISTS 'escalation_missed'")

    # --- users.availability_status -------------------------------------------
    availability = sa.Enum("available", "away", name="availability_status")
    availability.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "users",
        sa.Column(
            "availability_status",
            availability,
            nullable=False,
            server_default="available",
        ),
    )

    # --- tickets.acknowledged_at ---------------------------------------------
    op.add_column(
        "tickets",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- agent_devices --------------------------------------------------------
    # create_type=False: op.create_table below binds this same Enum object to a
    # column, and SQLAlchemy fires a table-bound "CREATE TYPE" DDL event for any
    # Enum column unless told not to — which would double-create the type after
    # the explicit .create() just below and abort the migration with
    # DuplicateObject. The explicit .create(checkfirst=True) call still runs
    # (create_type only gates the table-DDL auto-create event, not explicit
    # calls), so the type is created exactly once.
    platform = ENUM("android", "ios", name="device_platform", create_type=False)
    platform.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "agent_devices",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_token", sa.Text(), nullable=False),
        sa.Column("platform", platform, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "device_token", name="uq_agent_device_user_token"),
    )
    op.create_index("ix_agent_devices_id", "agent_devices", ["id"])
    op.create_index("ix_agent_devices_user_id", "agent_devices", ["user_id"])
    op.create_index("ix_agent_devices_tenant_id", "agent_devices", ["tenant_id"])
    op.create_index("ix_agent_devices_tenant_user", "agent_devices", ["tenant_id", "user_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_devices_tenant_user", table_name="agent_devices")
    op.drop_index("ix_agent_devices_tenant_id", table_name="agent_devices")
    op.drop_index("ix_agent_devices_user_id", table_name="agent_devices")
    op.drop_index("ix_agent_devices_id", table_name="agent_devices")
    op.drop_table("agent_devices")
    sa.Enum(name="device_platform").drop(op.get_bind(), checkfirst=True)
    op.drop_column("tickets", "acknowledged_at")
    op.drop_column("users", "availability_status")
    sa.Enum(name="availability_status").drop(op.get_bind(), checkfirst=True)
    # Postgres cannot drop enum values without recreating the type; the extra
    # ticket_status / notif_* values are inert when unused, so they stay.
