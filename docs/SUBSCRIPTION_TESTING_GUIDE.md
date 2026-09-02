# Subscription System Testing Guide

## ✅ Phase 1: Database Migration - COMPLETE

### Migration Status
- **Version**: `7d8e9f0a2b43`
- **Status**: ✅ Applied successfully
- **Date**: 2026-01-27

### Schema Changes Verified
✅ **Tenants Table**:
- `subscription_tier` (default: 'trial')
- `subscription_status` (default: 'trialing')
- `billing_cycle`, `trial_ends_at`, `subscription_starts_at`, `subscription_ends_at`
- Indexes created on tier, status, and trial_ends_at

✅ **Tenant Capabilities Table**:
- `max_knowledge_sources`, `can_use_url_sources`, `max_team_members`

✅ **Knowledge Sources Table**:
- `source_category`, `source_url`, `file_size_bytes`

---

## ✅ Test 1: Company Registration & Trial Setup - COMPLETE

### Test Result
```bash
curl -X POST http://localhost:8000/api/v1/auth/register-company \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "TestCompany",
    "company_display_name": "Test Company Inc",
    "admin_email": "admin@testcompany.com",
    "admin_full_name": "John Doe"
  }'
```

**Response**: ✅ 201 Created
```json
{
  "message": "Company registered successfully. Please check your email to verify your account.",
  "tenant_id": "71ca7137-8f94-4f11-8e81-2ac2d4126ed4",
  "admin_email": "admin@testcompany.com",
  "verification_required": true
}
```

### Trial Configuration Verified
✅ **Trial Period**: 30 days from signup
✅ **Trial Tier Capabilities**:
- Max Knowledge Sources: 2 (files only, no URLs)
- Can Use URL Sources: false
- Max Knowledge MB: 10 MB
- Max Conversations/Month: 100
- Max Team Members: 1 (admin only)
- Can Access Analytics: false
- Max Response Tokens: 800

---

## 🔄 Test 2: Knowledge Source Limits (IN PROGRESS)

### Test Steps

#### 2.1 Upload First File (Should Succeed - 1/2)
```bash
# TODO: Test file upload
```

#### 2.2 Upload Second File (Should Succeed - 2/2)
```bash
# TODO: Test file upload
```

#### 2.3 Upload Third File (Should Fail - Limit Reached)
```bash
# TODO: Test file upload - expect 403 error
```

#### 2.4 Try URL Source (Should Fail - Trial Tier)
```bash
curl -X POST http://localhost:8000/api/v1/knowledge/upload-url \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{
    "url": "https://example.com/faq",
    "tenant_id": "71ca7137-8f94-4f11-8e81-2ac2d4126ed4",
    "title": "FAQ Page"
  }'
```
**Expected**: 403 - "URL sources not enabled for trial tier"

---

## 🔄 Test 3: Trial Expiration (IN PROGRESS)

### Manual Trial Expiration
```sql
-- Manually expire trial for testing
UPDATE tenants
SET trial_ends_at = NOW() - INTERVAL '1 day'
WHERE name = 'testcompany';
```

### Test Blocked Endpoints
```bash
# Chat should be blocked
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{...}'
```
**Expected**: 402 Payment Required with upgrade message

---

## 🔄 Test 4: Subscription Upgrade (IN PROGRESS)

**⚠️ `POST /api/v1/subscription/upgrade` was removed 2026-08-22** (see
`docs/sop/capability_override_hardening.md`) — it let a tenant admin grant themselves any tier for
free, with no payment processing. The replacement for a manual/admin upgrade is the platform-admin
override endpoint below, which needs a super-admin token, not a tenant admin one.

### Upgrade to Professional Tier
```bash
curl -X POST http://localhost:8000/api/v1/platform/subscriptions/<TENANT_ID>/override \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <SUPER_ADMIN_TOKEN>" \
  -d '{
    "tier": "professional",
    "billing_cycle": "annual",
    "reason": "manual test upgrade via SUBSCRIPTION_TESTING_GUIDE",
    "apply_tier_capabilities": true
  }'
```

### Verify Capabilities Updated
```sql
SELECT
  max_knowledge_sources,    -- Should be 30
  can_use_url_sources,      -- Should be true
  max_knowledge_mb,         -- Should be 200
  max_conversations_per_month, -- Should be 5000
  max_team_members,         -- Should be 10
  can_access_analytics      -- Should be true
FROM tenant_capabilities tc
JOIN tenants t ON t.id = tc.tenant_id
WHERE t.name = 'testcompany';
```

---

## 💰 Paystack Payment Integration (TODO)

### Overview
Paystack will handle recurring payments for monthly and annual billing cycles.

### Integration Points

#### 1. Required Environment Variables
```env
PAYSTACK_SECRET_KEY=sk_test_xxxxxxxxxxxxx
PAYSTACK_PUBLIC_KEY=pk_test_xxxxxxxxxxxxx
PAYSTACK_CALLBACK_URL=https://luran.app/api/v1/subscription/paystack/callback
```

#### 2. Pricing Configuration (₦ Naira)
```python
PAYSTACK_PLANS = {
    "lite_monthly": {
        "plan_code": "PLN_lite_monthly",
        "amount": 10000,  # ₦10,000/month
        "interval": "monthly"
    },
    "lite_annual": {
        "plan_code": "PLN_lite_annual",
        "amount": 120000,  # ₦120,000/year (2 months free)
        "interval": "annually"
    },
    "professional_monthly": {
        "plan_code": "PLN_pro_monthly",
        "amount": 30000,  # ₦30,000/month
        "interval": "monthly"
    },
    "professional_annual": {
        "plan_code": "PLN_pro_annual",
        "amount": 350000,  # ₦350,000/year (2 months free)
        "interval": "annually"
    },
    "enterprise": {
        # Custom pricing - contact sales
        "contact_sales": true
    }
}
```

#### 3. Paystack Subscription Flow

**Step 1: Create Subscription Plan (One-time Setup)**
```bash
curl https://api.paystack.co/plan \
  -H "Authorization: Bearer SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Luran Lite Monthly",
    "amount": 1000000,  # Amount in kobo (₦10,000)
    "interval": "monthly",
    "currency": "NGN"
  }'
```

**Step 2: Initialize Transaction (User Upgrades)**
```python
# app/services/paystack_service.py
import httpx

class PaystackService:
    async def initialize_subscription(
        self,
        email: str,
        plan_code: str,
        metadata: dict
    ):
        response = await httpx.post(
            "https://api.paystack.co/transaction/initialize",
            headers={
                "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "email": email,
                "plan": plan_code,
                "metadata": metadata
            }
        )
        return response.json()
```

**Step 3: Handle Webhook (Payment Confirmed)**
```python
# app/api/v1/endpoints/paystack_webhook.py
@router.post("/paystack/webhook")
async def paystack_webhook(request: Request, db: Session = Depends(get_db)):
    # Verify signature
    signature = request.headers.get("x-paystack-signature")
    payload = await request.body()

    if not verify_paystack_signature(payload, signature):
        raise HTTPException(403, "Invalid signature")

    event = await request.json()

    if event["event"] == "subscription.create":
        # Activate subscription
        subscription_data = event["data"]
        await activate_subscription(
            db,
            customer_email=subscription_data["customer"]["email"],
            subscription_code=subscription_data["subscription_code"],
            plan_code=subscription_data["plan"]["plan_code"]
        )

    elif event["event"] == "charge.success":
        # Recurring payment successful
        pass

    elif event["event"] == "subscription.not_renew":
        # Subscription ending
        await handle_subscription_expiry(db, subscription_data)

    return {"status": "success"}
```

#### 4. Files to Create for Paystack Integration

1. **`app/services/paystack_service.py`** - Payment processing
   - `initialize_subscription()`
   - `verify_transaction()`
   - `cancel_subscription()`

2. **`app/api/v1/endpoints/paystack_webhook.py`** - Webhook handler
   - Verify webhook signatures
   - Handle payment events
   - Update subscription status

3. **`app/schemas/paystack.py`** - Payment schemas
   - `PaymentInitializeRequest`
   - `PaymentInitializeResponse`
   - `WebhookEvent`

4. **Update `app/api/v1/endpoints/subscription.py`**
   - Integrate Paystack payment flow
   - Redirect to Paystack payment page
   - Handle payment callback

#### 5. Subscription State Machine
```
TRIALING → (trial expires) → TRIAL_EXPIRED → (upgrade) → ACTIVE
ACTIVE → (payment fails) → PAST_DUE → (3 days grace) → CANCELED
ACTIVE → (user cancels) → CANCELED
ACTIVE → (payment succeeds) → ACTIVE (renewed)
```

#### 6. Security Considerations
- ✅ Verify all webhook signatures
- ✅ Use HTTPS for callbacks
- ✅ Store subscription_code for management
- ✅ Implement idempotency for webhooks
- ✅ Rate limit payment endpoints

---

## 📊 Tier Comparison Table

| Feature | Trial | Lite | Professional | Enterprise |
|---------|-------|------|--------------|------------|
| **Price** | Free (30 days) | ₦10k/mo<br>₦120k/yr | ₦30k/mo<br>₦350k/yr | Custom |
| **Knowledge Sources** | 2 | 10 | 30 | Unlimited |
| **URL Scraping** | ❌ | ✅ | ✅ | ✅ |
| **Storage** | 10 MB | 50 MB | 200 MB | Unlimited |
| **Conversations/Month** | 100 | 1,000 | 5,000 | Unlimited |
| **Team Members** | 1 | 3 | 10 | Unlimited |
| **Analytics** | ❌ | ✅ | ✅ | ✅ |
| **Max Response Tokens** | 800 | 1000 | 1500 | 2000 |
| **Support** | Email | Email | Priority Email | Dedicated |

---

## 🎯 Next Steps

### Immediate Testing (Today)
- [ ] Test knowledge source limit enforcement
- [ ] Test trial expiration blocking
- [ ] Test subscription upgrade flow
- [ ] Test conversation limits
- [ ] Test team member limits

### Paystack Integration (Next)
- [ ] Create Paystack account
- [ ] Set up subscription plans in Paystack dashboard
- [ ] Implement `PaystackService`
- [ ] Create webhook endpoint
- [ ] Add payment UI flow
- [ ] Test payment flow in sandbox mode
- [ ] Deploy webhook endpoint to production
- [ ] Configure Paystack webhook URL

### Production Readiness
- [ ] Add monitoring for failed payments
- [ ] Set up email notifications for trial expiry
- [ ] Create admin dashboard for subscription management
- [ ] Add usage analytics tracking
- [ ] Implement grace period for failed payments
- [ ] Add invoice generation
