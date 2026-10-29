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
from app.services.plan_catalog import build_plan_rows
from app.services.platform_admin_auth_service import PlatformAdminAuthService
from app.schemas.platform_admin import PlatformAdminCreate
from app.db.models.platform_admin import PlatformAdminRole
from app.core.config import settings
from loguru import logger


def create_subscription_plans(db):
    """Create subscription plans with pricing and capabilities."""

    # app/services/plan_catalog.py is the only definition of plan rows (see its docstring).
    plans_data = build_plan_rows()

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
