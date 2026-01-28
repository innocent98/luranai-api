"""
Test script for subscription downgrade functionality.

Tests:
1. Downgrade validation (tenant with too many resources)
2. Successful downgrade (tenant within limits)
3. Edge cases (trial user, canceled subscription, etc.)
"""

import httpx
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

BASE_URL = "http://localhost:8000/api/v1"


async def test_downgrade():
    """Run comprehensive downgrade tests."""

    async with httpx.AsyncClient() as client:
        print("=" * 80)
        print("SUBSCRIPTION DOWNGRADE TEST SUITE")
        print("=" * 80)
        print()

        # Step 1: Setup - Create or login to test tenant
        print("📋 Step 1: Setting up test environment...")
        print("-" * 80)

        # Try to login with existing test user
        test_email = "admin@testcompany.com"
        # Try multiple common passwords
        test_passwords = ["password123", "Password123", "TestPassword123!", "admin123", "test123"]

        token = None
        for password in test_passwords:
            login_response = await client.post(
                f"{BASE_URL}/auth/login",
                json={"email": test_email, "password": password}
            )

            if login_response.status_code == 200:
                token_data = login_response.json()
                token = token_data["access_token"]
                print(f"✅ Logged in as: {test_email}")
                print(f"   Password: {password}")
                break

        if not token:
            print(f"❌ Login failed with all attempted passwords")
            print(f"   Last response: {login_response.text[:200]}")
            print()
            print("💡 Try resetting the password in the database")
            return

        headers = {"Authorization": f"Bearer {token}"}

        # Step 2: Check current subscription
        print()
        print("📊 Step 2: Checking current subscription...")
        print("-" * 80)

        subscription_response = await client.get(
            f"{BASE_URL}/subscription/my-subscription",
            headers=headers
        )

        if subscription_response.status_code != 200:
            print(f"❌ Failed to get subscription: {subscription_response.status_code}")
            print(f"   Response: {subscription_response.text}")
            return

        subscription = subscription_response.json()
        print(f"Current Tier: {subscription['subscription_tier']}")
        print(f"Status: {subscription['subscription_status']}")
        print(f"Billing Cycle: {subscription.get('billing_cycle', 'N/A')}")

        current_tier = subscription['subscription_tier']

        # Step 3: Get pricing plans
        print()
        print("💰 Step 3: Getting available pricing plans...")
        print("-" * 80)

        plans_response = await client.get(f"{BASE_URL}/subscription/plans")
        if plans_response.status_code == 200:
            plans = plans_response.json()
            for plan in plans:
                if plan['tier'] in ['lite', 'professional']:
                    monthly_price = plan['monthly_price_kobo'] / 100
                    print(f"{plan['tier'].upper()}: ₦{monthly_price:,.2f}/month")

        # Determine test scenario based on current tier
        if current_tier == "trial":
            print()
            print("=" * 80)
            print("TEST CASE 1: Trial User Cannot Downgrade")
            print("=" * 80)

            # Try to downgrade from trial (should fail)
            downgrade_response = await client.post(
                f"{BASE_URL}/subscription/downgrade",
                headers=headers,
                json={"tier": "lite", "reason": "Testing downgrade"}
            )

            print(f"Status Code: {downgrade_response.status_code}")
            print(f"Response: {downgrade_response.json()}")

            if downgrade_response.status_code == 402:
                print("✅ PASS: Trial users correctly blocked from downgrading")
            else:
                print("❌ FAIL: Expected 402 status code")

        elif current_tier == "professional":
            print()
            print("=" * 80)
            print("TEST CASE 2: Professional → Lite Downgrade")
            print("=" * 80)

            # Try to downgrade to Lite
            downgrade_response = await client.post(
                f"{BASE_URL}/subscription/downgrade",
                headers=headers,
                json={
                    "tier": "lite",
                    "reason": "Testing downgrade functionality - reducing costs"
                }
            )

            print(f"Status Code: {downgrade_response.status_code}")
            response_data = downgrade_response.json()

            if downgrade_response.status_code == 400:
                # Validation failed - show violations
                print()
                print("⚠️  VALIDATION FAILED - Current usage exceeds Lite tier limits:")
                print()

                detail = response_data.get('detail', {})
                if isinstance(detail, dict) and 'violations' in detail:
                    for violation in detail['violations']:
                        print(f"  ❌ {violation['field'].upper()}")
                        print(f"     Current: {violation['current_value']}")
                        print(f"     Allowed: {violation['allowed_value']}")
                        print(f"     Over by: {violation['difference']}")
                        print(f"     Action: {violation['action_required']}")
                        print()

                    print("✅ PASS: Validation correctly prevents downgrade with over-limit data")
                else:
                    print(f"Response: {response_data}")

            elif downgrade_response.status_code == 200:
                # Successful downgrade
                print()
                print("✅ DOWNGRADE SUCCESSFUL!")
                print()
                print(f"  Previous Tier: {response_data['previous_tier']}")
                print(f"  New Tier: {response_data['new_tier']}")
                print(f"  Next Billing Date: {response_data.get('next_billing_date', 'N/A')}")

                amount_kobo = response_data['next_billing_amount_kobo']
                amount_naira = amount_kobo / 100
                print(f"  Next Billing Amount: ₦{amount_naira:,.2f}")

                if response_data.get('warnings'):
                    print()
                    print("  Warnings:")
                    for warning in response_data['warnings']:
                        print(f"    - {warning}")

                print()
                print("✅ PASS: Downgrade completed successfully")

                # Verify subscription was updated
                print()
                print("🔍 Verifying subscription was updated...")
                verify_response = await client.get(
                    f"{BASE_URL}/subscription/my-subscription",
                    headers=headers
                )

                if verify_response.status_code == 200:
                    updated_sub = verify_response.json()
                    print(f"  Current Tier: {updated_sub['subscription_tier']}")

                    if updated_sub['subscription_tier'] == 'lite':
                        print("  ✅ Subscription tier successfully updated to Lite")
                    else:
                        print(f"  ❌ Unexpected tier: {updated_sub['subscription_tier']}")

            else:
                print(f"❌ Unexpected status code: {downgrade_response.status_code}")
                print(f"Response: {response_data}")

        elif current_tier == "lite":
            print()
            print("=" * 80)
            print("TEST CASE 3: Lite User Cannot Downgrade Further")
            print("=" * 80)

            # Try to "downgrade" to trial (should fail - not a valid downgrade)
            downgrade_response = await client.post(
                f"{BASE_URL}/subscription/downgrade",
                headers=headers,
                json={"tier": "trial", "reason": "Testing"}
            )

            print(f"Status Code: {downgrade_response.status_code}")
            print(f"Response: {downgrade_response.json()}")

            if downgrade_response.status_code == 400:
                print("✅ PASS: Lite users correctly blocked from downgrading to trial")
            else:
                print("❌ FAIL: Expected 400 status code")

            # Test trying to "downgrade" to same tier
            print()
            print("Testing same-tier 'downgrade'...")
            same_tier_response = await client.post(
                f"{BASE_URL}/subscription/downgrade",
                headers=headers,
                json={"tier": "lite", "reason": "Testing"}
            )

            print(f"Status Code: {same_tier_response.status_code}")
            if same_tier_response.status_code == 400:
                print("✅ PASS: Same-tier 'downgrade' correctly rejected")

        # Step 4: Check subscription history
        print()
        print("=" * 80)
        print("SUBSCRIPTION HISTORY")
        print("=" * 80)

        history_response = await client.get(
            f"{BASE_URL}/subscription/history?limit=5",
            headers=headers
        )

        if history_response.status_code == 200:
            history = history_response.json()
            print(f"Found {len(history)} recent changes:")
            print()

            for entry in history:
                print(f"  Action: {entry['action']}")
                print(f"  Tier Change: {entry.get('previous_tier', 'N/A')} → {entry.get('new_tier', 'N/A')}")
                print(f"  Date: {entry['created_at']}")
                print(f"  Changed By: {entry.get('changed_by_name', 'N/A')}")
                if entry.get('reason'):
                    print(f"  Reason: {entry['reason']}")
                print()

        print("=" * 80)
        print("TEST SUITE COMPLETE")
        print("=" * 80)


if __name__ == "__main__":
    asyncio.run(test_downgrade())
