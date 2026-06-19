"""create business_profiles table

Revision ID: b3c4d5e6f7g8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b3c4d5e6f7g8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_profiles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("industry", sa.String(120), nullable=True),
        sa.Column("offerings", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("audience", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("positioning", sa.Text, nullable=True),
        sa.Column("key_entities", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("tone", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "source",
            sa.Enum("auditor", "manual", name="business_profile_source"),
            nullable=False,
            server_default="auditor",
        ),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("model_version", sa.String(80), nullable=True),
        sa.Column("raw", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_business_profiles_tenant_id",
        "business_profiles",
        ["tenant_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_business_profiles_tenant_id", table_name="business_profiles")
    op.drop_table("business_profiles")
    op.execute("DROP TYPE IF EXISTS business_profile_source")
