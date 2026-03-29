"""create automation actions and executions

Revision ID: j0k1l2m3n4o5
Revises: i8j9k0l1m2n3
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "j0k1l2m3n4o5"
down_revision = "i8j9k0l1m2n3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use explicit DO blocks so migration is idempotent across environments
    # where enum types may already exist.
    op.execute(
        "DO $$ BEGIN CREATE TYPE tenant_action_status AS ENUM "
        "('active', 'inactive', 'archived'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.execute(
        "DO $$ BEGIN CREATE TYPE tenant_action_type AS ENUM ('http'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.execute(
        "DO $$ BEGIN CREATE TYPE action_auth_type AS ENUM "
        "('none', 'bearer', 'api_key_header'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.execute(
        "DO $$ BEGIN CREATE TYPE action_trigger_source AS ENUM "
        "('flow_node', 'manual_test'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.execute(
        "DO $$ BEGIN CREATE TYPE action_execution_status AS ENUM "
        "('queued', 'running', 'succeeded', 'failed', 'timed_out', 'blocked_policy'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )

    tenant_action_status = postgresql.ENUM(
        "active",
        "inactive",
        "archived",
        name="tenant_action_status",
        create_type=False,
    )
    tenant_action_type = postgresql.ENUM(
        "http",
        name="tenant_action_type",
        create_type=False,
    )
    action_auth_type = postgresql.ENUM(
        "none",
        "bearer",
        "api_key_header",
        name="action_auth_type",
        create_type=False,
    )
    action_trigger_source = postgresql.ENUM(
        "flow_node",
        "manual_test",
        name="action_trigger_source",
        create_type=False,
    )
    action_execution_status = postgresql.ENUM(
        "queued",
        "running",
        "succeeded",
        "failed",
        "timed_out",
        "blocked_policy",
        name="action_execution_status",
        create_type=False,
    )

    op.create_table(
        "tenant_actions",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", tenant_action_status, nullable=False),
        sa.Column("type", tenant_action_type, nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("url_template", sa.Text(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("auth_type", action_auth_type, nullable=False),
        sa.Column("auth_config", sa.JSON(), nullable=False),
        sa.Column("headers_template", sa.JSON(), nullable=False),
        sa.Column("query_template", sa.JSON(), nullable=False),
        sa.Column("body_template", sa.JSON(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("success_condition", sa.JSON(), nullable=True),
        sa.Column("error_mapping", sa.JSON(), nullable=False),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("is_sensitive", sa.Boolean(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("allow_in_flows", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_actions_id", "tenant_actions", ["id"], unique=False)
    op.create_index("ix_tenant_actions_status", "tenant_actions", ["status"], unique=False)
    op.create_index("ix_tenant_actions_tenant_id", "tenant_actions", ["tenant_id"], unique=False)
    op.create_index(
        "ix_tenant_actions_tenant_slug", "tenant_actions", ["tenant_id", "slug"], unique=True
    )
    op.create_index(
        "ix_tenant_actions_tenant_status",
        "tenant_actions",
        ["tenant_id", "status"],
        unique=False,
    )

    op.create_table(
        "action_executions",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("flow_execution_id", sa.UUID(), nullable=True),
        sa.Column("flow_id", sa.UUID(), nullable=True),
        sa.Column("node_id", sa.String(length=100), nullable=True),
        sa.Column("action_id", sa.UUID(), nullable=False),
        sa.Column("trigger_source", action_trigger_source, nullable=False),
        sa.Column("status", action_execution_status, nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("resolved_request", sa.JSON(), nullable=False),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column("response_headers", sa.JSON(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("normalized_output", sa.JSON(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("approval_status", sa.String(length=20), nullable=True),
        sa.Column("approved_by", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["action_id"], ["tenant_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["flow_execution_id"], ["flow_executions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["flow_id"], ["flows.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_action_executions_action_id", "action_executions", ["action_id"], unique=False
    )
    op.create_index(
        "ix_action_executions_action_created",
        "action_executions",
        ["action_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_action_executions_flow_execution_id",
        "action_executions",
        ["flow_execution_id"],
        unique=False,
    )
    op.create_index("ix_action_executions_flow_id", "action_executions", ["flow_id"], unique=False)
    op.create_index("ix_action_executions_id", "action_executions", ["id"], unique=False)
    op.create_index(
        "ix_action_executions_session_created",
        "action_executions",
        ["session_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_action_executions_session_id", "action_executions", ["session_id"], unique=False
    )
    op.create_index("ix_action_executions_status", "action_executions", ["status"], unique=False)
    op.create_index(
        "ix_action_executions_tenant_created",
        "action_executions",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_action_executions_tenant_id", "action_executions", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_action_executions_tenant_id", table_name="action_executions")
    op.drop_index("ix_action_executions_tenant_created", table_name="action_executions")
    op.drop_index("ix_action_executions_status", table_name="action_executions")
    op.drop_index("ix_action_executions_session_id", table_name="action_executions")
    op.drop_index("ix_action_executions_session_created", table_name="action_executions")
    op.drop_index("ix_action_executions_id", table_name="action_executions")
    op.drop_index("ix_action_executions_flow_id", table_name="action_executions")
    op.drop_index("ix_action_executions_flow_execution_id", table_name="action_executions")
    op.drop_index("ix_action_executions_action_created", table_name="action_executions")
    op.drop_index("ix_action_executions_action_id", table_name="action_executions")
    op.drop_table("action_executions")

    op.drop_index("ix_tenant_actions_tenant_status", table_name="tenant_actions")
    op.drop_index("ix_tenant_actions_tenant_slug", table_name="tenant_actions")
    op.drop_index("ix_tenant_actions_tenant_id", table_name="tenant_actions")
    op.drop_index("ix_tenant_actions_status", table_name="tenant_actions")
    op.drop_index("ix_tenant_actions_id", table_name="tenant_actions")
    op.drop_table("tenant_actions")

    bind = op.get_bind()
    sa.Enum(name="action_execution_status").drop(bind, checkfirst=True)
    sa.Enum(name="action_trigger_source").drop(bind, checkfirst=True)
    sa.Enum(name="action_auth_type").drop(bind, checkfirst=True)
    sa.Enum(name="tenant_action_type").drop(bind, checkfirst=True)
    sa.Enum(name="tenant_action_status").drop(bind, checkfirst=True)
