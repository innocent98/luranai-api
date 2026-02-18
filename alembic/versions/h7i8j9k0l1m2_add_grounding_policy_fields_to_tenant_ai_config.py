"""Add grounding policy fields to tenant_ai_configs

Revision ID: h7i8j9k0l1m2
Revises: g5h6i7j8k9l0
Create Date: 2026-02-18 10:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "h7i8j9k0l1m2"
down_revision = "g5h6i7j8k9l0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "grounding_mode",
            sa.String(length=20),
            nullable=False,
            server_default="balanced",
            comment="Grounding strictness: strict, balanced, flexible",
        ),
    )
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "low_confidence_action",
            sa.String(length=20),
            nullable=False,
            server_default="clarify",
            comment="Action when retrieval confidence is low: clarify, escalate, refuse",
        ),
    )
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "no_retrieval_action",
            sa.String(length=20),
            nullable=False,
            server_default="clarify",
            comment="Action when no knowledge is retrieved: clarify, escalate, refuse",
        ),
    )

    op.alter_column("tenant_ai_configs", "grounding_mode", server_default=None)
    op.alter_column("tenant_ai_configs", "low_confidence_action", server_default=None)
    op.alter_column("tenant_ai_configs", "no_retrieval_action", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_ai_configs", "no_retrieval_action")
    op.drop_column("tenant_ai_configs", "low_confidence_action")
    op.drop_column("tenant_ai_configs", "grounding_mode")

