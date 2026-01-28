"""Create a test user for downgrade testing."""

import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.tenant import Tenant, TenantCapabilities
from app.db.models.user import User, UserRole
from app.services.auth_service import AuthService


async def create_test_setup():
    """Create test tenant and user for downgrade testing."""

    db = next(get_db())

    try:
        # Check if test tenant exists
        tenant = db.query(Tenant).filter(Tenant.name == "test_company").first()

        if not tenant:
            print("Creating test tenant...")

            # Create tenant with Professional tier
            tenant = Tenant(
                name="test_company",
                display_name="Test Company",
                description="Test tenant for downgrade functionality",
                subscription_tier="professional",
                subscription_status="active",
                billing_cycle="monthly",
                subscription_starts_at=datetime.now(timezone.utc) - timedelta(days=15),
                api_key_hash="test_hash_" + "x" * 50,
            )
            db.add(tenant)
            db.flush()

            # Create capabilities for Professional tier
            capabilities = TenantCapabilities(
                tenant_id=tenant.id,
                can_access_analytics=True,
                max_knowledge_sources=30,
                can_use_url_sources=True,
                max_knowledge_mb=500,
                max_conversations_per_month=10000,
                max_team_members=20,
                max_response_tokens=2000,
                can_upload_knowledge=True,
            )
            db.add(capabilities)

            print(f"✅ Created tenant: {tenant.name} (ID: {tenant.id})")
        else:
            print(f"✅ Found existing tenant: {tenant.name} (ID: {tenant.id})")
            print(f"   Tier: {tenant.subscription_tier}")
            print(f"   Status: {tenant.subscription_status}")

        # Check if test user exists
        auth_service = AuthService(db)
        test_email = "admin@testcompany.com"

        user = db.query(User).filter(User.email == test_email).first()

        if not user:
            print()
            print("Creating test user...")

            hashed_password = auth_service.get_password_hash("TestPassword123!")

            user = User(
                email=test_email,
                hashed_password=hashed_password,
                full_name="Test Admin",
                role=UserRole.TENANT_ADMIN,
                tenant_id=tenant.id,
                is_active=True,
            )
            db.add(user)

            print(f"✅ Created user: {test_email}")
            print(f"   Password: TestPassword123!")
        else:
            print()
            print(f"✅ Found existing user: {test_email}")

        db.commit()

        print()
        print("=" * 80)
        print("TEST ENVIRONMENT READY")
        print("=" * 80)
        print()
        print(f"Tenant: {tenant.name}")
        print(f"Tier: {tenant.subscription_tier}")
        print(f"Status: {tenant.subscription_status}")
        print()
        print(f"Login Email: {test_email}")
        print(f"Password: TestPassword123!")
        print()
        print("You can now run: poetry run python test_downgrade.py")
        print()

    except Exception as e:
        print(f"❌ Error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(create_test_setup())
