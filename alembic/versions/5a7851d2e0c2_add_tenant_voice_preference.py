"""add tenant voice preference

Revision ID: 5a7851d2e0c2
Revises: d1e2mergevoicecaller
Create Date: 2026-08-21 16:24:11.217880

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5a7851d2e0c2'
down_revision = 'd1e2mergevoicecaller'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "voice_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="Tenant's own on/off for voice chat (grant is TenantCapabilities.can_use_voice)",
        ),
    )
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "voice_id",
            sa.String(length=64),
            nullable=True,
            comment="Chosen ElevenLabs voice id; null = platform default",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_ai_configs", "voice_id")
    op.drop_column("tenant_ai_configs", "voice_enabled")
