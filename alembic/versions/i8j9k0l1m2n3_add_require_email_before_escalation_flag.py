"""add require_email_before_escalation flag to tenant_ai_configs

Revision ID: i8j9k0l1m2n3
Revises: h7i8j9k0l1m2
Create Date: 2026-02-20 11:55:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "i8j9k0l1m2n3"
down_revision = "h7i8j9k0l1m2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "require_email_before_escalation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Require capturing customer email before escalating to human support",
        ),
    )
    op.alter_column("tenant_ai_configs", "require_email_before_escalation", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_ai_configs", "require_email_before_escalation")

