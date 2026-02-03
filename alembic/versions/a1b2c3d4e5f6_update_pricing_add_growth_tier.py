"""Update pricing and add growth tier

Revision ID: a1b2c3d4e5f6
Revises: f3d55906a9b1
Create Date: 2026-02-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = ('3081c68284fa', '9f8a1b2c3d4e')  # Merge heads
branch_labels = None
depends_on = None


def upgrade():
    """Update pricing for existing tiers and add Growth tier."""
    connection = op.get_bind()

    # Trial: 100 → 200 conversations
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET max_conversations_per_month = 200
        WHERE tier = 'trial'
        """)
    )

    # Lite: ₦12,000 → ₦9,000, 1,000 → 2,000 conversations
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET
            monthly_price_kobo = 900000,
            annual_price_kobo = 9000000,
            max_conversations_per_month = 2000,
            max_knowledge_sources = 15
        WHERE tier = 'lite'
        """)
    )

    # Professional: ₦35,000 → ₦40,000, 5,000 → 10,000 conversations
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET
            monthly_price_kobo = 4000000,
            annual_price_kobo = 40000000,
            max_conversations_per_month = 10000,
            max_knowledge_sources = 50
        WHERE tier = 'professional'
        """)
    )

    # Insert Growth tier
    connection.execute(
        text("""
        INSERT INTO subscription_plans (
            id, tier, display_name, description, sort_order, is_active,
            monthly_price_kobo, annual_price_kobo,
            features,
            can_access_analytics, max_knowledge_sources, can_use_url_sources,
            max_knowledge_mb, max_conversations_per_month, max_team_members,
            max_response_tokens, created_at, updated_at
        ) VALUES (
            gen_random_uuid(),
            'growth',
            'Growth',
            'Perfect for scaling businesses with growing customer support needs',
            3,
            true,
            8000000,
            80000000,
            '["50 knowledge sources", "500MB storage", "25,000 conversations/month", "20 team members", "URL scraping", "Advanced analytics", "Priority support", "Custom integrations"]'::jsonb,
            true,
            50,
            true,
            500,
            25000,
            20,
            1500,
            NOW(),
            NOW()
        )
        """)
    )

    # Update Enterprise sort_order and description
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET
            sort_order = 4,
            description = 'Volume-based pricing for high-scale operations (25K+ conversations)',
            features = '["Unlimited knowledge sources", "Unlimited storage", "Volume-based pricing (25K-300K+)", "Unlimited team members", "Dedicated account manager", "Custom integrations", "SLA guarantees", "24/7 premium support"]'::jsonb
        WHERE tier = 'enterprise'
        """)
    )


def downgrade():
    """Revert pricing changes and remove Growth tier."""
    connection = op.get_bind()

    # Revert Trial
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET max_conversations_per_month = 100
        WHERE tier = 'trial'
        """)
    )

    # Revert Lite
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET
            monthly_price_kobo = 1200000,
            annual_price_kobo = 12000000,
            max_conversations_per_month = 1000,
            max_knowledge_sources = 10
        WHERE tier = 'lite'
        """)
    )

    # Revert Professional
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET
            monthly_price_kobo = 3500000,
            annual_price_kobo = 35000000,
            max_conversations_per_month = 5000,
            max_knowledge_sources = 30
        WHERE tier = 'professional'
        """)
    )

    # Revert Enterprise sort_order and description
    connection.execute(
        text("""
        UPDATE subscription_plans
        SET
            sort_order = 3,
            description = 'Contact us for custom pricing and unlimited features',
            features = '["Unlimited knowledge sources", "Unlimited storage", "Unlimited conversations", "Unlimited team members", "URL scraping", "Analytics dashboard", "Premium AI responses", "24/7 priority support", "Dedicated account manager", "Custom integrations"]'::jsonb
        WHERE tier = 'enterprise'
        """)
    )

    # Remove Growth tier
    connection.execute(text("DELETE FROM subscription_plans WHERE tier = 'growth'"))
