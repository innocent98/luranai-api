"""add action_executions tenant idempotency index

Revision ID: 2b3514240056
Revises: 0c1be6a3a289
Create Date: 2026-06-16 14:53:14.037354

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '2b3514240056'
down_revision = '0c1be6a3a289'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_action_executions_tenant_idem",
        "action_executions",
        ["tenant_id", "idempotency_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_action_executions_tenant_idem", table_name="action_executions")
