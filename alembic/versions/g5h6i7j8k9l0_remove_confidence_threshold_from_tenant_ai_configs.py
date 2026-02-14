"""remove confidence_threshold from tenant_ai_configs

Revision ID: g5h6i7j8k9l0
Revises: f4g5h6i7j8k9
Create Date: 2026-02-14 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "g5h6i7j8k9l0"
down_revision: str | None = "f4g5h6i7j8k9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop deprecated, unused AI confidence field."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("tenant_ai_configs")}
    if "confidence_threshold" in columns:
        op.drop_column("tenant_ai_configs", "confidence_threshold")


def downgrade() -> None:
    """Restore deprecated AI confidence field for rollback compatibility."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("tenant_ai_configs")}
    if "confidence_threshold" not in columns:
        op.add_column(
            "tenant_ai_configs",
            sa.Column(
                "confidence_threshold",
                sa.Float(),
                nullable=False,
                server_default=sa.text("0.55"),
            ),
        )
        op.alter_column("tenant_ai_configs", "confidence_threshold", server_default=None)
