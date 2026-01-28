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
from loguru import logger


def create_subscription_plans(db):
    """Create subscription plans with pricing and capabilities."""

    plans_data = [
        {
            "tier": "trial",
            "display_name": "Trial",
            "description": "Try out the platform with limited features",
            "monthly_price_kobo": 0,
            "annual_price_kobo": 0,
            "can_access_analytics": False,
            "max_knowledge_sources": 2,
            "can_use_url_sources": False,
            "max_knowledge_mb": 10,
            "max_conversations_per_month": 100,
            "max_team_members": 1,
            "max_response_tokens": 800,
            "features": [
                "2 knowledge sources",
                "10MB storage",
                "100 conversations/month",
                "1 team member",
                "Email support",
            ],
            "is_active": True,
            "sort_order": 0,
        },
        {
            "tier": "lite",
            "display_name": "Lite",
            "description": "Perfect for small businesses getting started",
            "monthly_price_kobo": 1200000,  # ₦12,000/month
            "annual_price_kobo": 12000000,  # ₦120,000/year
            "can_access_analytics": True,
            "max_knowledge_sources": 10,
            "can_use_url_sources": True,
            "max_knowledge_mb": 50,
            "max_conversations_per_month": 1000,
            "max_team_members": 3,
            "max_response_tokens": 1000,
            "features": [
                "10 knowledge sources",
                "50MB storage",
                "1,000 conversations/month",
                "3 team members",
                "URL scraping",
                "Analytics dashboard",
                "Priority email support",
            ],
            "is_active": True,
            "sort_order": 1,
        },
        {
            "tier": "professional",
            "display_name": "Professional",
            "description": "Advanced features for growing teams",
            "monthly_price_kobo": 3500000,  # ₦35,000/month
            "annual_price_kobo": 35000000,  # ₦350,000/year
            "can_access_analytics": True,
            "max_knowledge_sources": 30,
            "can_use_url_sources": True,
            "max_knowledge_mb": 200,
            "max_conversations_per_month": 5000,
            "max_team_members": 10,
            "max_response_tokens": 1500,
            "features": [
                "30 knowledge sources",
                "200MB storage",
                "5,000 conversations/month",
                "10 team members",
                "URL scraping",
                "Analytics dashboard",
                "Advanced AI responses",
                "Priority support",
            ],
            "is_active": True,
            "sort_order": 2,
        },
        {
            "tier": "enterprise",
            "display_name": "Enterprise",
            "description": "Contact us for custom pricing and unlimited features",
            "monthly_price_kobo": 0,  # Contact for pricing
            "annual_price_kobo": 0,  # Contact for pricing
            "can_access_analytics": True,
            "max_knowledge_sources": None,  # Unlimited
            "can_use_url_sources": True,
            "max_knowledge_mb": None,  # Unlimited
            "max_conversations_per_month": None,  # Unlimited
            "max_team_members": None,  # Unlimited
            "max_response_tokens": 2000,
            "features": [
                "Unlimited knowledge sources",
                "Unlimited storage",
                "Unlimited conversations",
                "Unlimited team members",
                "URL scraping",
                "Analytics dashboard",
                "Premium AI responses",
                "24/7 priority support",
                "Dedicated account manager",
                "Custom integrations",
            ],
            "is_active": True,
            "sort_order": 3,
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
    """Create Luran platform admin."""

    platform_admin_service = PlatformAdminAuthService(db)

    admin_data = PlatformAdminCreate(
        email="admin@luranai.com",
        password="admin123",
        full_name="Luran Platform Admin",
        role=PlatformAdminRole.SUPER_ADMIN,
    )

    try:
        platform_admin = platform_admin_service.create_admin(admin_data)
        logger.success(f"Created platform admin: {platform_admin.email}")
        return True
    except Exception as e:
        if "already exists" in str(e).lower():
            logger.info("Platform admin already exists: admin@luranai.com")
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
        logger.success("  Email: admin@luranai.com")
        logger.success("  Password: admin123")
        logger.success("=" * 50 + "\n")

    except Exception as e:
        logger.error(f"Error seeding database: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
