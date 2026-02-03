"""Fix features display for updated pricing

Revision ID: b7c8d9e0f1g2
Revises: a1b2c3d4e5f6
Create Date: 2026-02-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = 'b7c8d9e0f1g2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    """Update features arrays to match new pricing structure."""
    connection = op.get_bind()

    # Trial: Update features to show 200 conversations
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET features = '[
            "2 knowledge sources",
            "10MB storage",
            "200 conversations/month",
            "1 team member",
            "Email support"
        ]'::jsonb
        WHERE tier = 'trial'
        """)
    )

    # Lite: Update features to show 15 sources and 2,000 conversations
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET features = '[
            "15 knowledge sources",
            "50MB storage",
            "2,000 conversations/month",
            "3 team members",
            "URL scraping",
            "Analytics dashboard",
            "Priority email support"
        ]'::jsonb
        WHERE tier = 'lite'
        """)
    )

    # Professional: Update features to show 50 sources and 10,000 conversations
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET features = '[
            "50 knowledge sources",
            "200MB storage",
            "10,000 conversations/month",
            "10 team members",
            "URL scraping",
            "Analytics dashboard",
            "Advanced AI responses",
            "Priority support"
        ]'::jsonb
        WHERE tier = 'professional'
        """)
    )


def downgrade():
    """Revert features arrays to old values."""
    connection = op.get_bind()

    # Revert Trial
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET features = '[
            "2 knowledge sources",
            "10MB storage",
            "100 conversations/month",
            "1 team member",
            "Email support"
        ]'::jsonb
        WHERE tier = 'trial'
        """)
    )

    # Revert Lite
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET features = '[
            "10 knowledge sources",
            "50MB storage",
            "1,000 conversations/month",
            "3 team members",
            "URL scraping",
            "Analytics dashboard",
            "Priority email support"
        ]'::jsonb
        WHERE tier = 'lite'
        """)
    )

    # Revert Professional
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET features = '[
            "30 knowledge sources",
            "200MB storage",
            "5,000 conversations/month",
            "10 team members",
            "URL scraping",
            "Analytics dashboard",
            "Advanced AI responses",
            "Priority support"
        ]'::jsonb
        WHERE tier = 'professional'
        """)
    )
