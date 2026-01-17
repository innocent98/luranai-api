"""Quick script to check existing users for testing."""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.core.config import settings
from app.db.models.user import User, UserRole
from app.db.models.tenant import Tenant

# Create engine
engine = create_engine(str(settings.DATABASE_URL))

with Session(engine) as db:
    # Get all users
    users = db.execute(select(User)).scalars().all()

    if not users:
        print("❌ No users found in database")
        print("\nYou need to create a user first. Options:")
        print("1. Register a company via API: POST /api/v1/auth/register-company")
        print("2. Use existing test data if available")
        sys.exit(1)

    print(f"✅ Found {len(users)} users:\n")

    for user in users:
        tenant = db.get(Tenant, user.tenant_id)
        print(f"Email: {user.email}")
        print(f"Name: {user.full_name}")
        print(f"Role: {user.role.value}")
        print(f"Tenant: {tenant.display_name if tenant else 'N/A'} ({user.tenant_id})")
        print(f"Active: {user.is_active}")
        print("-" * 60)

    # Show tenant admins and support agents separately
    admins = [u for u in users if u.role == UserRole.TENANT_ADMIN]
    agents = [u for u in users if u.role == UserRole.SUPPORT_AGENT]

    print(f"\n📊 Summary:")
    print(f"  Tenant Admins: {len(admins)}")
    print(f"  Support Agents: {len(agents)}")
    print(f"  Total Active: {sum(1 for u in users if u.is_active)}")

    if admins:
        print(f"\n✅ You can login as:")
        for admin in admins[:3]:  # Show first 3
            print(f"  - {admin.email} (TENANT_ADMIN)")

    if agents:
        print(f"\n✅ Support agents available:")
        for agent in agents[:3]:
            print(f"  - {agent.email} (SUPPORT_AGENT)")
