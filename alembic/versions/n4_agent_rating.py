"""N4 agent rating: rating_requests, per-party session_ratings, encrypted comments

Revision ID: n4agentrating
Revises: sr10screenrec
Create Date: 2026-10-13

Adds rating_requests (token-gated, hash-only) and session_ratings.rated_agent_id /
source / rating_request_id. Swaps "one rating per session" for "one rating per
(session, rated party)" via two partial unique indexes. Backfills source from the
legacy meta->>'source'. Encrypts every existing comment in place (unconditional: pre-N4 values are always plaintext), so the
model's EncryptedString and the data can never be out of step (spec N4-D7).
Requires ENCRYPTION_KEY. Downgrade refuses while any session has >1 rating.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "n4agentrating"
down_revision = "sr10screenrec"
branch_labels = None
depends_on = None

SESSION_RATING_SOURCE_CHECK = (
    "source IN ('idle_prompt', 'resolution_prompt', 'manual_request', 'auto_resolve')"
)
RATING_REQUEST_SOURCE_CHECK = "source IN ('manual_request', 'auto_resolve')"
SOURCE_CASE_SQL = (
    "CASE WHEN meta->>'source' IN ('agent_resolved', 'ticket_closed') "
    "THEN 'resolution_prompt' ELSE 'idle_prompt' END"
)
BACKFILL_SOURCE_SQL = f"UPDATE session_ratings SET source = {SOURCE_CASE_SQL} WHERE source IS NULL"
DUPLICATE_SESSIONS_SQL = (
    "SELECT count(*) FROM (SELECT session_id FROM session_ratings "
    "GROUP BY session_id HAVING count(*) > 1) AS d"
)
_BATCH = 500


def _require_encryption_key() -> None:
    if not os.environ.get("ENCRYPTION_KEY"):
        raise RuntimeError(
            "n4agentrating needs ENCRYPTION_KEY in the environment (session_ratings.comment "
            "is encrypted in this migration). Run alembic from the API container."
        )


def _rewrite_comments(bind, transform) -> int:
    """Keyset-walk non-NULL comments; transform(value) returns the new value."""
    changed = 0
    undecryptable = 0
    last_id = None
    while True:
        if last_id is None:
            rows = bind.execute(
                sa.text(
                    "SELECT id, comment FROM session_ratings WHERE comment IS NOT NULL "
                    "ORDER BY id LIMIT :n"
                ),
                {"n": _BATCH},
            ).all()
        else:
            rows = bind.execute(
                sa.text(
                    "SELECT id, comment FROM session_ratings WHERE comment IS NOT NULL "
                    "AND id > :last ORDER BY id LIMIT :n"
                ),
                {"last": last_id, "n": _BATCH},
            ).all()
        if not rows:
            if undecryptable:
                raise RuntimeError(
                    f"n4agentrating: {undecryptable} comment(s) could not be "
                    "transformed with the current key. Check ENCRYPTION_KEY / "
                    "ENCRYPTION_KEY_PREVIOUS; nothing was committed."
                )
            return changed
        for row_id, value in rows:
            try:
                new_value = transform(value)
            except Exception:  # noqa: BLE001 — never include the value or key in output
                undecryptable += 1
                continue
            if new_value is not None and new_value != value:
                bind.execute(
                    sa.text("UPDATE session_ratings SET comment = :c WHERE id = :id"),
                    {"c": new_value, "id": row_id},
                )
                changed += 1
        last_id = rows[-1][0]


def _encrypt_one(value: str) -> str:
    """Pre-N4 the column is always plaintext, so encrypt unconditionally (no shape sniffing)."""
    from app.core.encryption import encrypt_field

    return encrypt_field(value)


def _decrypt_one(value: str) -> str:
    """Post-upgrade the app writes only encrypted comments; failure here means a wrong key."""
    from app.core.encryption import decrypt_field

    return decrypt_field(value)


def encrypt_existing_comments(bind) -> int:
    return _rewrite_comments(bind, _encrypt_one)


def decrypt_existing_comments(bind) -> int:
    return _rewrite_comments(bind, _decrypt_one)


def upgrade() -> None:
    _require_encryption_key()
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.create_table(
        "rating_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.CheckConstraint(RATING_REQUEST_SOURCE_CHECK, name="ck_rating_requests_source"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rating_requests_id", "rating_requests", ["id"])
    op.create_index("ix_rating_requests_token_hash", "rating_requests", ["token_hash"], unique=True)
    op.create_index(
        "ix_rating_requests_session_agent_created",
        "rating_requests",
        ["session_id", "agent_id", "created_at"],
    )
    op.create_index(
        "ix_rating_requests_tenant_created", "rating_requests", ["tenant_id", "created_at"]
    )

    op.add_column("session_ratings", sa.Column("rated_agent_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_session_ratings_rated_agent_id_users",
        "session_ratings",
        "users",
        ["rated_agent_id"],
        ["id"],
    )
    op.add_column("session_ratings", sa.Column("source", sa.String(length=32), nullable=True))
    op.add_column("session_ratings", sa.Column("rating_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_session_ratings_rating_request_id",
        "session_ratings",
        "rating_requests",
        ["rating_request_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_session_ratings_rating_request_id", "session_ratings", ["rating_request_id"]
    )

    op.execute(BACKFILL_SOURCE_SQL)
    op.alter_column("session_ratings", "source", existing_type=sa.String(length=32), nullable=False)
    op.create_check_constraint(
        "ck_session_ratings_source", "session_ratings", SESSION_RATING_SOURCE_CHECK
    )

    # Uniqueness swap: one rating per session -> one per (session, rated party).
    op.execute(
        "ALTER TABLE session_ratings DROP CONSTRAINT IF EXISTS uq_session_rating_per_session"
    )
    op.execute("DROP INDEX IF EXISTS ix_session_ratings_session_id")
    op.create_index("ix_session_ratings_session_id", "session_ratings", ["session_id"])
    op.create_index(
        "uq_session_ratings_session_agent",
        "session_ratings",
        ["session_id", "rated_agent_id"],
        unique=True,
        postgresql_where=sa.text("rated_agent_id IS NOT NULL"),
    )
    op.create_index(
        "uq_session_ratings_session_ai",
        "session_ratings",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("rated_agent_id IS NULL"),
    )
    op.create_index(
        "ix_session_ratings_tenant_agent_created",
        "session_ratings",
        ["tenant_id", "rated_agent_id", "created_at"],
    )

    bind = op.get_bind()
    leftover = bind.execute(
        sa.text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'session_ratings' "
            "AND indexdef LIKE 'CREATE UNIQUE INDEX%' AND indexdef NOT LIKE '% WHERE %' "
            "AND indexdef LIKE '%(session_id)'"
        )
    ).all()
    if leftover:
        raise RuntimeError(
            "n4agentrating: a non-partial unique index on session_ratings(session_id) "
            f"remains: {[r[0] for r in leftover]}"
        )

    encrypt_existing_comments(bind)


def downgrade() -> None:
    _require_encryption_key()
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(DUPLICATE_SESSIONS_SQL)).scalar()
    if duplicates:
        raise RuntimeError(
            f"n4agentrating downgrade blocked: {duplicates} session(s) have more than one "
            "rating. De-duplicate first (keep the AI row) — docs/sop/n4_agent_rating_backend.md."
        )
    decrypt_existing_comments(bind)

    op.drop_index("ix_session_ratings_tenant_agent_created", table_name="session_ratings")
    op.drop_index("uq_session_ratings_session_ai", table_name="session_ratings")
    op.drop_index("uq_session_ratings_session_agent", table_name="session_ratings")
    op.drop_index("ix_session_ratings_session_id", table_name="session_ratings")
    op.create_index("ix_session_ratings_session_id", "session_ratings", ["session_id"], unique=True)
    op.create_unique_constraint("uq_session_rating_per_session", "session_ratings", ["session_id"])
    op.drop_constraint("ck_session_ratings_source", "session_ratings", type_="check")
    op.drop_constraint("uq_session_ratings_rating_request_id", "session_ratings", type_="unique")
    op.drop_constraint(
        "fk_session_ratings_rating_request_id", "session_ratings", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_session_ratings_rated_agent_id_users", "session_ratings", type_="foreignkey"
    )
    op.drop_column("session_ratings", "rating_request_id")
    op.drop_column("session_ratings", "source")
    op.drop_column("session_ratings", "rated_agent_id")
    op.drop_table("rating_requests")
