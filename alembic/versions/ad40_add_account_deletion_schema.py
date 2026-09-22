"""account deletion: deletion_status/deletion_requested_at on users, deletion_requests table

Revision ID: ad40accountdeletion
Revises: d1f2humanagentname
Create Date: 2026-09-22

Task 1 of the account-deletion backend feature (see
.superpowers/sdd/2026-09-22-account-deletion-backend/). Schema only — no
service/endpoint/worker code lands in this migration.

Lock/size note: `users` and `deletion_requests` are both small-to-moderate
tables today (no bulk backfill here — `deletion_status` is a constant
server_default, so Postgres 11+ adds it without a table rewrite). Expected
lock duration on prod-sized data: sub-second ShareUpdateExclusiveLock-class
DDL, not a blocking rewrite.

Enum casing: every enum here is created with EXPLICIT LOWERCASE LABELS that
match `values_callable=lambda x: [e.value for e in x]` on the corresponding
SQLAlchemy model column (see app/db/models/user.py:DeletionStatus and
app/db/models/deletion_request.py:DeletionScope/DeletionRequestedVia). This is
deliberate: a prior migration (3a83f8e6d080) added an enum VALUE that didn't
match the NAME SQLAlchemy's default (no values_callable) `Enum(...)` emits on
insert, breaking every insert that used it until d1f2humanagentname fixed it.
Round-tripped against a real Postgres 17 instance: created a User row with
every DeletionStatus member and a DeletionRequest row with every
scope/status/requested_via member — all four enums round-tripped correctly.

Enum type objects use `sqlalchemy.dialects.postgresql.ENUM` (not the generic
`sa.Enum`) with `create_type=False` on every column reference, mirroring
`ac30agentcaller`'s `device_platform` pattern: the type is created exactly
once via an explicit `.create(checkfirst=True)` call, and `create_type=False`
on each column stops SQLAlchemy from re-emitting `CREATE TYPE` when the
column's table is built. The generic `sa.Enum(..., create_type=False)` was
tried first and does NOT reliably suppress the table-bound auto-create event
inside `op.create_table` for every column in a multi-enum table (reproduced:
it re-attempted `CREATE TYPE deletion_scope` and failed with
`DuplicateObject` even though `create_type=False` was set) — the
dialect-native `postgresql.ENUM` does not have this problem.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

# revision identifiers, used by Alembic.
revision = "ad40accountdeletion"
down_revision = "d1f2humanagentname"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # --- enum types (explicit lowercase labels, matching values_callable) ----
    deletion_status = ENUM(
        "active",
        "pending_verification",
        "grace",
        "purged",
        name="deletion_status",
        create_type=False,
    )
    deletion_scope = ENUM(
        "self_user",
        "tenant",
        name="deletion_scope",
        create_type=False,
    )
    deletion_requested_via = ENUM(
        "authed",
        "public",
        name="deletion_requested_via",
        create_type=False,
    )
    deletion_status.create(bind, checkfirst=True)
    deletion_scope.create(bind, checkfirst=True)
    deletion_requested_via.create(bind, checkfirst=True)

    # --- users.deletion_status / users.deletion_requested_at -----------------
    op.add_column(
        "users",
        sa.Column(
            "deletion_status",
            deletion_status,
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "users",
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- deletion_requests -----------------------------------------------------
    op.create_table(
        "deletion_requests",
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
        sa.Column("scope", deletion_scope, nullable=False),
        sa.Column(
            "status",
            deletion_status,
            nullable=False,
            server_default="pending_verification",
        ),
        sa.Column("verification_token_hash", sa.String(length=64), nullable=True),
        sa.Column("cancel_token_hash", sa.String(length=64), nullable=True),
        sa.Column("requested_via", deletion_requested_via, nullable=False),
        sa.Column("scheduled_purge_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_deletion_requests_id", "deletion_requests", ["id"])
    op.create_index("ix_deletion_requests_user_id", "deletion_requests", ["user_id"])
    op.create_index("ix_deletion_requests_tenant_id", "deletion_requests", ["tenant_id"])
    op.create_index(
        "ix_deletion_requests_status_scheduled_purge",
        "deletion_requests",
        ["status", "scheduled_purge_at"],
    )
    op.create_index(
        "ix_deletion_requests_verification_token_hash",
        "deletion_requests",
        ["verification_token_hash"],
    )
    op.create_index(
        "ix_deletion_requests_cancel_token_hash",
        "deletion_requests",
        ["cancel_token_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_deletion_requests_cancel_token_hash", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_verification_token_hash", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_status_scheduled_purge", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_tenant_id", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_user_id", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_id", table_name="deletion_requests")
    op.drop_table("deletion_requests")

    op.drop_column("users", "deletion_requested_at")
    op.drop_column("users", "deletion_status")

    bind = op.get_bind()
    ENUM(name="deletion_requested_via", create_type=False).drop(bind, checkfirst=True)
    ENUM(name="deletion_scope", create_type=False).drop(bind, checkfirst=True)
    ENUM(name="deletion_status", create_type=False).drop(bind, checkfirst=True)
