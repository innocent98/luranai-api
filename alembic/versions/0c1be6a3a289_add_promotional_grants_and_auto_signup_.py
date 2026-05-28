"""add promotional grants and auto signup config

Revision ID: 0c1be6a3a289
Revises: db9f7c863585
Create Date: 2026-05-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0c1be6a3a289"
down_revision = "db9f7c863585"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promotional_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_code", sa.String(64), nullable=False),
        sa.Column("campaign_name", sa.String(160), nullable=True),
        sa.Column("granted_tier", sa.String(50), nullable=False),
        sa.Column("duration_days", sa.Integer, nullable=False),
        sa.Column("grant_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_tier", sa.String(50), nullable=False),
        sa.Column("previous_status", sa.String(50), nullable=False),
        sa.Column("previous_billing_cycle", sa.String(20), nullable=True),
        sa.Column("previous_trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_subscription_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text, nullable=True),
        sa.Column("granted_by_admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("platform_admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("granted_by_admin_name", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.create_index(
        "promo_grants_one_active_per_tenant",
        "promotional_grants",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "promo_grants_idempotency",
        "promotional_grants",
        ["tenant_id", "campaign_code"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'converted')"),
    )
    op.create_index(
        "promo_grants_expiry_scan",
        "promotional_grants",
        ["grant_ends_at", "status"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("promo_grants_campaign_lookup", "promotional_grants", ["campaign_code", "status"])
    op.create_index("promo_grants_tenant_lookup", "promotional_grants", ["tenant_id", sa.text("created_at DESC")])

    op.create_table(
        "auto_signup_grant_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("campaign_code", sa.String(64), nullable=True),
        sa.Column("campaign_name", sa.String(160), nullable=True),
        sa.Column("granted_tier", sa.String(50), nullable=True),
        sa.Column("duration_days", sa.Integer, nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_by_admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("platform_admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by_admin_name", sa.String(255), nullable=True),
    )

    op.execute(
        "CREATE UNIQUE INDEX auto_signup_grant_config_singleton "
        "ON auto_signup_grant_config((true))"
    )

    # Seed singleton row
    op.execute(
        "INSERT INTO auto_signup_grant_config (enabled) VALUES (false)"
    )


def downgrade() -> None:
    op.drop_index("auto_signup_grant_config_singleton", table_name="auto_signup_grant_config")
    op.drop_table("auto_signup_grant_config")
    op.drop_index("promo_grants_tenant_lookup", table_name="promotional_grants")
    op.drop_index("promo_grants_campaign_lookup", table_name="promotional_grants")
    op.drop_index("promo_grants_expiry_scan", table_name="promotional_grants")
    op.drop_index("promo_grants_idempotency", table_name="promotional_grants")
    op.drop_index("promo_grants_one_active_per_tenant", table_name="promotional_grants")
    op.drop_table("promotional_grants")
