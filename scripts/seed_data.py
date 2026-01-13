"""
Seed database with initial test data.

Creates:
- Test tenant (Weysure)
- Superuser
- Tenant admin
- AI configuration
- Sample knowledge document
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from uuid import UUID
from app.db.session import SessionLocal
from app.services.auth_service import AuthService
from app.services.tenant_service import TenantService
from app.services.knowledge_service import KnowledgeService
from app.schemas.user import UserCreate
from app.schemas.tenant import TenantCreate, TenantAIConfigUpdate
from app.db.models.knowledge import SourceType
from app.db.models.user import UserRole
from loguru import logger


# Sample insurance FAQ document
SAMPLE_FAQ = """
What is covered under comprehensive insurance?

Comprehensive insurance covers damage to your vehicle that is not caused by a collision. This includes:
- Theft and vandalism
- Fire and explosions
- Natural disasters (floods, earthquakes, hurricanes)
- Falling objects (tree branches, hail)
- Animal strikes (hitting a deer)
- Glass damage (windshield cracks)

Comprehensive coverage is optional but highly recommended for protecting your investment.

How do I file a claim?

Filing a claim is simple and can be done in several ways:
1. Call our 24/7 claims hotline at 1-800-INSURANCE
2. Use our mobile app to submit photos and details
3. Visit any of our local offices
4. File online through your customer portal

You'll need your policy number, date and location of incident, and any relevant documentation like police reports or photos.

What is the deductible?

A deductible is the amount you pay out-of-pocket before your insurance coverage kicks in. For example, if you have a $500 deductible and file a $3000 claim, you pay $500 and we cover the remaining $2500.

Common deductible amounts:
- $250 (higher premiums, lower out-of-pocket)
- $500 (balanced option)
- $1000 (lower premiums, higher out-of-pocket)

You can adjust your deductible during renewal to balance premium costs.

Can I add drivers to my policy?

Yes! You can add additional drivers to your policy at any time. Common scenarios:
- Adding a teenage driver (may increase premiums)
- Adding a spouse or partner
- Adding elderly parents

Each driver's record will be evaluated and may affect your premium. To add a driver, contact your agent or use the online portal.

What happens if I miss a payment?

If you miss a payment, here's what happens:
1. Grace period: You have 10 days from the due date to make payment without penalty
2. Late fee: A $25 late fee is applied after the grace period
3. Coverage lapse: After 30 days of non-payment, coverage may be suspended
4. Policy cancellation: After 60 days, the policy may be cancelled

We'll send multiple reminders via email and text. Set up autopay to avoid missing payments!

How do I update my policy information?

You can update your policy information in several ways:
- Online through your customer portal (available 24/7)
- Call our customer service at 1-800-INSURANCE
- Visit any local office
- Email your agent directly

Common updates include: address changes, vehicle information, driver additions/removals, coverage adjustments, and payment methods.

What is the claims process timeline?

Our typical claims timeline:
- Initial report: Immediate (24/7 availability)
- Adjuster contact: Within 24-48 hours
- Inspection scheduled: Within 3-5 business days
- Initial estimate: Within 7 business days
- Repair authorization: Within 2-3 days after estimate approval
- Payment issued: Within 5 business days after completed repairs

Complex claims may take longer. We'll keep you updated throughout the process.
"""


async def main():
    """Seed the database with initial data."""
    db = SessionLocal()

    try:
        logger.info("=" * 60)
        logger.info("Starting database seeding...")
        logger.info("=" * 60)

        auth_service = AuthService(db)
        tenant_service = TenantService(db)
        knowledge_service = KnowledgeService(db)

        # Step 1: Create superuser
        logger.info("\n1. Creating superuser...")
        try:
            superuser_data = UserCreate(
                email="admin@luran.dev",
                password="admin123",  # Change in production!
                full_name="Super Admin",
                role=UserRole.SUPER_ADMIN,
            )
            superuser = auth_service.create_user(superuser_data)
            logger.success(f"✓ Created superuser: {superuser.email}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.warning("Superuser already exists, skipping...")
            else:
                raise

        # Step 2: Create test tenant (Weysure)
        logger.info("\n2. Creating test tenant (Weysure)...")
        try:
            tenant_data = TenantCreate(
                name="weysure",
                display_name="Weysure Insurance",
                description="AI-powered insurance support for Weysure customers",
            )
            tenant = tenant_service.create_tenant(tenant_data)
            tenant_id = tenant.id
            logger.success(f"✓ Created tenant: {tenant.display_name} (ID: {tenant_id})")
            logger.info(f"  API Key: {tenant.api_key[:20]}...")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.warning("Tenant already exists, fetching...")
                tenant = tenant_service.get_tenant_by_name("weysure")
                if not tenant:
                    raise Exception("Could not find or create Weysure tenant")
                tenant_id = tenant.id
                logger.info(f"  Using existing tenant ID: {tenant_id}")
            else:
                raise

        # Step 3: Create tenant admin user
        logger.info("\n3. Creating tenant admin user...")
        try:
            admin_data = UserCreate(
                email="admin@weysure.com",
                password="weysure123",  # Change in production!
                full_name="Weysure Admin",
                role=UserRole.TENANT_ADMIN,
                tenant_id=tenant_id,
            )
            tenant_admin = auth_service.create_user(admin_data)
            logger.success(f"✓ Created tenant admin: {tenant_admin.email}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.warning("Tenant admin already exists, skipping...")
            else:
                raise

        # Step 4: Configure AI settings
        logger.info("\n4. Configuring AI settings...")
        ai_config_data = TenantAIConfigUpdate(
            brand_voice={
                "tone": "professional",
                "formality_level": "balanced",
                "greeting_style": "warm",
                "emoji_usage": False,
            },
            allowed_topics=[
                "insurance coverage",
                "claims process",
                "policy information",
                "billing and payments",
                "account management",
            ],
            blocked_topics=[
                "personal medical advice",
                "legal advice",
                "investment advice",
            ],
            escalation_keywords=[
                "lawyer",
                "lawsuit",
                "complaint",
                "supervisor",
                "cancel policy",
                "fraud",
            ],
            confidence_threshold=0.55,
            max_response_tokens=400,
            grounding_requirement=True,
            custom_instructions="Always be empathetic when discussing claims. Remind users about our 24/7 claims hotline for urgent matters.",
        )
        ai_config = tenant_service.update_ai_config(tenant_id, ai_config_data)
        logger.success("✓ AI configuration updated")
        logger.info(f"  Confidence threshold: {ai_config.confidence_threshold}")
        logger.info(f"  Max tokens: {ai_config.max_response_tokens}")

        # Step 5: Upload knowledge document
        logger.info("\n5. Uploading sample knowledge document...")
        try:
            source = await knowledge_service.process_document(
                tenant_id=tenant_id,
                document_text=SAMPLE_FAQ,
                document_name="insurance_faq.txt",
                source_type=SourceType.FAQ,
                document_metadata={
                    "language": "en",
                    "category": "general",
                    "author": "support_team",
                },
            )
            logger.success(f"✓ Uploaded knowledge document: {source.name}")
            logger.info(f"  Source ID: {source.id}")
            logger.info(f"  Version: {source.version}")
            logger.info(f"  Chunks created: {source.chunk_count}")
            logger.info(f"  Status: {source.status}")
        except Exception as e:
            logger.error(f"✗ Failed to upload knowledge: {e}")
            logger.warning("  (May be due to API quota limits)")

        # Print summary
        logger.info("\n" + "=" * 60)
        logger.success("Database seeding completed!")
        logger.info("=" * 60)
        logger.info("\nTest Credentials:")
        logger.info("  Superuser:")
        logger.info("    Email: admin@luran.dev")
        logger.info("    Password: admin123")
        logger.info("\n  Tenant Admin (Weysure):")
        logger.info("    Email: admin@weysure.com")
        logger.info("    Password: weysure123")
        logger.info(f"\n  Tenant ID: {tenant_id}")
        logger.info("\nNext Steps:")
        logger.info("  1. Start the server: poetry run uvicorn app.main:app --reload")
        logger.info("  2. Visit API docs: http://localhost:8000/api/v1/docs")
        logger.info("  3. Login to get JWT token")
        logger.info("  4. Test chat endpoint with uploaded knowledge")

    except Exception as e:
        logger.error(f"Error during seeding: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
