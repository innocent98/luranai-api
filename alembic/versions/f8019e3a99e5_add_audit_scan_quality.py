"""add audit scan_quality

Revision ID: f8019e3a99e5
Revises: 9b886c489f29
Create Date: 2026-07-08 08:50:28.696098

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f8019e3a99e5'
down_revision = '9b886c489f29'
branch_labels = None
depends_on = None

SCAN_QUALITY = sa.Enum(
    "ok", "thin", "unreachable", "render_failed", name="audit_scan_quality"
)


def upgrade() -> None:
    bind = op.get_bind()
    SCAN_QUALITY.create(bind, checkfirst=True)
    op.add_column(
        "audit_reports",
        sa.Column("scan_quality", SCAN_QUALITY, nullable=True),
    )
    op.add_column(
        "audit_reports",
        sa.Column("scan_quality_reason", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_reports", "scan_quality_reason")
    op.drop_column("audit_reports", "scan_quality")
    SCAN_QUALITY.drop(op.get_bind(), checkfirst=True)
