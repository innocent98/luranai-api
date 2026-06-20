"""add auditor capability columns

Revision ID: a2b3c4d5e6f7
Revises: 2b3514240056
Create Date: 2026-06-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "2b3514240056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscription_plans", sa.Column("max_audit_pages_per_crawl", sa.Integer(), nullable=True))
    op.add_column("subscription_plans", sa.Column("max_audits_per_month", sa.Integer(), nullable=True))
    op.add_column("tenant_capabilities", sa.Column("max_audit_pages_per_crawl", sa.Integer(), nullable=True))
    op.add_column("tenant_capabilities", sa.Column("max_audits_per_month", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE subscription_plans SET
            max_audit_pages_per_crawl = CASE tier
                WHEN 'trial' THEN 50 WHEN 'lite' THEN 200
                WHEN 'professional' THEN 500 WHEN 'growth' THEN 500 ELSE NULL END,
            max_audits_per_month = CASE tier
                WHEN 'trial' THEN 2 WHEN 'lite' THEN 5
                WHEN 'professional' THEN 15 WHEN 'growth' THEN 30 ELSE NULL END
        """
    )
    op.execute(
        """
        UPDATE tenant_capabilities tc
        SET max_audit_pages_per_crawl = sp.max_audit_pages_per_crawl,
            max_audits_per_month = sp.max_audits_per_month
        FROM tenants t
        JOIN subscription_plans sp ON sp.tier = t.subscription_tier
        WHERE tc.tenant_id = t.id
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "max_audits_per_month")
    op.drop_column("tenant_capabilities", "max_audit_pages_per_crawl")
    op.drop_column("subscription_plans", "max_audits_per_month")
    op.drop_column("subscription_plans", "max_audit_pages_per_crawl")
