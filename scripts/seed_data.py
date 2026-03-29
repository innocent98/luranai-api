"""
Seed database with initial data.

Creates:
- Subscription plans (trial, lite, professional, enterprise)
- Platform admin (admin@luranai.com)
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.db.session import SessionLocal
from app.db.models.subscription_plan import SubscriptionPlan
from app.services.platform_admin_auth_service import PlatformAdminAuthService
from app.schemas.platform_admin import PlatformAdminCreate
from app.db.models.platform_admin import PlatformAdminRole
from app.core.config import settings
from loguru import logger


def create_subscription_plans(db):
    """Create subscription plans with pricing and capabilities."""

    plans_data = [
        {
            "tier": "trial",
            "display_name": "Trial",
            "description": "Try Luran with core AI support and starter limits.",
            "monthly_price_kobo": 0,
            "annual_price_kobo": 0,
            "can_access_analytics": False,
            "max_knowledge_sources": 2,
            "can_use_url_sources": False,
            "max_knowledge_mb": 10,
            "max_conversations_per_month": 200,
            "max_team_members": 1,
            "max_response_tokens": 450,
            "max_automation_actions": 2,
            "max_automation_executions_per_month": 100,
            "can_use_ai_chat_automation": True,
            "can_use_lookup_chains": False,
            "max_flows": 1,
            "max_nodes_per_flow": 0,
            "can_use_webhooks": False,
            "max_webhooks_per_flow": 0,
            "features": [
                "2 knowledge sources",
                "10MB storage",
                "200 conversations/month",
                "1 team member",
                "1 flow",
                "2 automation actions",
                "100 automation executions/month",
                "AI chat automation",
                "AI support widget",
                "Email support",
            ],
            "is_active": True,
            "sort_order": 0,
        },
        {
            "tier": "lite",
            "display_name": "Lite",
            "description": "For small teams getting started.",
            "monthly_price_kobo": 900000,  # ₦9,000/month
            "annual_price_kobo": 9000000,  # ₦90,000/year
            "can_access_analytics": True,
            "max_knowledge_sources": 15,
            "can_use_url_sources": True,
            "max_knowledge_mb": 50,
            "max_conversations_per_month": 2000,
            "max_team_members": 3,
            "max_response_tokens": 800,
            "max_automation_actions": 5,
            "max_automation_executions_per_month": 500,
            "can_use_ai_chat_automation": True,
            "can_use_lookup_chains": False,
            "max_flows": 3,
            "max_nodes_per_flow": 20,
            "can_use_webhooks": False,
            "max_webhooks_per_flow": 0,
            "features": [
                "15 knowledge sources",
                "50MB storage",
                "2,000 conversations/month",
                "3 team members",
                "3 flows",
                "5 automation actions",
                "500 automation executions/month",
                "AI chat automation",
                "URL scraping",
                "Analytics dashboard",
                "Full support",
            ],
            "is_active": True,
            "sort_order": 1,
        },
        {
            "tier": "professional",
            "display_name": "Professional",
            "description": "Advanced features for growing teams",
            "monthly_price_kobo": 4000000,  # ₦40,000/month
            "annual_price_kobo": 40000000,  # ₦400,000/year
            "can_access_analytics": True,
            "max_knowledge_sources": 50,
            "can_use_url_sources": True,
            "max_knowledge_mb": 200,
            "max_conversations_per_month": 10000,
            "max_team_members": 10,
            "max_response_tokens": 1200,
            "max_automation_actions": 15,
            "max_automation_executions_per_month": 5000,
            "can_use_ai_chat_automation": True,
            "can_use_lookup_chains": True,
            "max_flows": 10,
            "max_nodes_per_flow": 50,
            "can_use_webhooks": True,
            "max_webhooks_per_flow": 3,
            "features": [
                "50 knowledge sources",
                "200MB storage",
                "10,000 conversations/month",
                "10 team members",
                "10 flows",
                "15 automation actions",
                "5,000 automation executions/month",
                "AI chat automation",
                "Lookup chains",
                "Webhook integrations",
                "URL scraping",
                "Analytics dashboard",
                "Full support",
            ],
            "is_active": True,
            "sort_order": 2,
        },
        {
            "tier": "growth",
            "display_name": "Growth",
            "description": "Perfect for scaling businesses with growing customer support needs",
            "monthly_price_kobo": 8000000,  # ₦80,000/month
            "annual_price_kobo": 80000000,  # ₦800,000/year
            "can_access_analytics": True,
            "max_knowledge_sources": 50,
            "can_use_url_sources": True,
            "max_knowledge_mb": 500,
            "max_conversations_per_month": 25000,
            "max_team_members": 20,
            "max_response_tokens": 1400,
            "max_automation_actions": 40,
            "max_automation_executions_per_month": 25000,
            "can_use_ai_chat_automation": True,
            "can_use_lookup_chains": True,
            "max_flows": 25,
            "max_nodes_per_flow": 100,
            "can_use_webhooks": True,
            "max_webhooks_per_flow": 10,
            "features": [
                "50 knowledge sources",
                "500MB storage",
                "25,000 conversations/month",
                "20 team members",
                "25 flows",
                "40 automation actions",
                "25,000 automation executions/month",
                "AI chat automation",
                "Lookup chains",
                "Up to 10 webhooks per flow",
                "URL scraping",
                "Analytics dashboard",
                "Full support",
            ],
            "is_active": True,
            "sort_order": 3,
        },
        {
            "tier": "enterprise",
            "display_name": "Enterprise",
            "description": "Volume-based pricing for high-scale operations (25K+ conversations)",
            "monthly_price_kobo": 0,  # Contact for pricing
            "annual_price_kobo": 0,  # Contact for pricing
            "can_access_analytics": True,
            "max_knowledge_sources": None,  # Unlimited
            "can_use_url_sources": True,
            "max_knowledge_mb": None,  # Unlimited
            "max_conversations_per_month": None,  # Unlimited
            "max_team_members": None,  # Unlimited
            "max_response_tokens": 1600,
            "max_automation_actions": None,  # Unlimited
            "max_automation_executions_per_month": None,  # Unlimited
            "can_use_ai_chat_automation": True,
            "can_use_lookup_chains": True,
            "max_flows": None,  # Unlimited
            "max_nodes_per_flow": None,  # Unlimited
            "can_use_webhooks": True,
            "max_webhooks_per_flow": None,  # Unlimited
            "features": [
                "Unlimited knowledge sources",
                "Unlimited storage",
                "Volume-based pricing (25K-300K+)",
                "Unlimited team members",
                "Unlimited flows",
                "Unlimited automation actions",
                "Custom automation executions/month",
                "AI chat automation",
                "Lookup chains",
                "Custom webhook limits",
                "Dedicated account manager",
                "Custom integrations",
                "SLA guarantees",
                "24/7 premium support",
            ],
            "is_active": True,
            "sort_order": 4,
        },
    ]

    created_count = 0
    updated_count = 0

    for plan_data in plans_data:
        # Check if plan already exists
        existing = db.query(SubscriptionPlan).filter(
            SubscriptionPlan.tier == plan_data["tier"]
        ).first()

        if existing:
            # Update existing plan
            for key, value in plan_data.items():
                if key != "tier":  # Don't update tier
                    setattr(existing, key, value)
            updated_count += 1
            logger.info(f"Updated subscription plan: {plan_data['tier']}")
        else:
            # Create new plan
            plan = SubscriptionPlan(**plan_data)
            db.add(plan)
            created_count += 1
            logger.info(f"Created subscription plan: {plan_data['tier']}")

    db.commit()
    return created_count, updated_count


def create_platform_admin(db):
    """Create Luran platform admin from environment variables."""
    platform_admin_service = PlatformAdminAuthService(db)

    admin_data = PlatformAdminCreate(
        email=settings.FIRST_SUPERUSER_EMAIL,
        password=settings.FIRST_SUPERUSER_PASSWORD,
        full_name="Luran Platform Admin",
        role=PlatformAdminRole.SUPER_ADMIN,
    )

    try:
        platform_admin = platform_admin_service.create_admin(admin_data)
        logger.success(f"Created platform admin: {platform_admin.email}")
        return True
    except Exception as e:
        if "already exists" in str(e).lower():
            logger.info(f"Platform admin already exists: {settings.FIRST_SUPERUSER_EMAIL}")
            return False
        else:
            logger.error(f"Failed to create platform admin: {e}")
            raise


def main():
    """Main seeding function."""
    logger.info("Starting database seeding...")

    db = SessionLocal()
    try:
        # Step 1: Create subscription plans
        logger.info("Creating subscription plans...")
        created, updated = create_subscription_plans(db)
        logger.success(
            f"Subscription plans: {created} created, {updated} updated"
        )

        # Step 2: Create platform admin
        logger.info("Creating platform admin...")
        admin_created = create_platform_admin(db)
        if admin_created:
            logger.success("Platform admin created")
        else:
            logger.info("Platform admin already exists")

        # Summary
        logger.success("\n" + "=" * 50)
        logger.success("Database seeding completed successfully!")
        logger.success("=" * 50)
        logger.success("\nCreated:")
        logger.success(f"  - {created} new subscription plans")
        logger.success(f"  - {updated} updated subscription plans")
        if admin_created:
            logger.success("  - 1 platform admin")
        logger.success("\nPlatform Admin Credentials:")
        logger.success(f"  Email: {settings.FIRST_SUPERUSER_EMAIL}")
        logger.success("  Password: (from FIRST_SUPERUSER_PASSWORD env var)")
        logger.success("=" * 50 + "\n")

    except Exception as e:
        logger.error(f"Error seeding database: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
