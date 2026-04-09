"""add customer user foundation

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-04-03 12:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("chat_user_key", sa.String(length=255), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("avatar_url", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("chat_user_key"),
    )
    op.create_index(op.f("ix_customer_users_id"), "customer_users", ["id"], unique=False)
    op.create_index(op.f("ix_customer_users_email"), "customer_users", ["email"], unique=True)
    op.create_index(
        op.f("ix_customer_users_chat_user_key"),
        "customer_users",
        ["chat_user_key"],
        unique=True,
    )
    op.create_index(
        "ix_customer_users_email_active",
        "customer_users",
        ["email", "is_active"],
        unique=False,
    )
    op.create_index(op.f("ix_customer_users_is_active"), "customer_users", ["is_active"], unique=False)

    op.add_column(
        "conversation_sessions",
        sa.Column("customer_user_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "ix_conversation_sessions_customer",
        "conversation_sessions",
        ["customer_user_id", "last_message_at"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_conversation_sessions_customer_user_id",
        "conversation_sessions",
        "customer_users",
        ["customer_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_conversation_sessions_customer_user_id", "conversation_sessions", type_="foreignkey")
    op.drop_index("ix_conversation_sessions_customer", table_name="conversation_sessions")
    op.drop_column("conversation_sessions", "customer_user_id")

    op.drop_index(op.f("ix_customer_users_is_active"), table_name="customer_users")
    op.drop_index(op.f("ix_customer_users_chat_user_key"), table_name="customer_users")
    op.drop_index("ix_customer_users_email_active", table_name="customer_users")
    op.drop_index(op.f("ix_customer_users_email"), table_name="customer_users")
    op.drop_index(op.f("ix_customer_users_id"), table_name="customer_users")
    op.drop_table("customer_users")
