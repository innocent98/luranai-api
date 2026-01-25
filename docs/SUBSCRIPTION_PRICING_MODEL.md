# Luran Subscription & Pricing Model Documentation

**Last Updated:** 2026-01-25
**Status:** Current Implementation (v1.0)

---

## Table of Contents

1. [Overview](#overview)
2. [Current Implementation](#current-implementation)
3. [Tenant Capabilities System](#tenant-capabilities-system)
4. [AI Configuration Limits](#ai-configuration-limits)
5. [Feature Access Control](#feature-access-control)
6. [Resource Limits](#resource-limits)
7. [Default Configuration](#default-configuration)
8. [API Endpoints](#api-endpoints)
9. [Enforcement Points](#enforcement-points)
10. [Recommended Pricing Tiers](#recommended-pricing-tiers)
11. [Implementation Gaps & Future Work](#implementation-gaps--future-work)

---

## 1. Overview

Luran implements a **capability-based subscription system** where each tenant has:
- **Feature permissions** (can/cannot use specific features)
- **Resource limits** (max storage, conversations, etc.)
- **AI configuration** (token limits, confidence thresholds)

**Key Philosophy:**
- Capabilities are **configurable per tenant** by platform admins
- **No hardcoded pricing tiers** in the current implementation
- All tenants start with generous defaults
- Platform admins can adjust limits via API

---

## 2. Current Implementation

### What's Built:

#### ✅ Database Models
- `TenantCapabilities` table with feature flags and limits
- `TenantAIConfig` table with AI behavior settings
- All capabilities stored per tenant in PostgreSQL

#### ✅ API Endpoints
- Platform admin endpoints to view/update tenant capabilities
- Tenant endpoints to view own capabilities (read-only)
- Enforcement in specific endpoints (analytics, etc.)

#### ✅ Feature Flags
- `can_create_tickets`: Enable/disable ticket escalation
- `can_access_analytics`: Enable/disable analytics dashboard
- `can_upload_knowledge`: Enable/disable knowledge uploads

#### ✅ Resource Limits
- `max_knowledge_mb`: Maximum knowledge base storage in MB
- `max_conversations_per_month`: Monthly conversation limit (nullable)
- `max_response_tokens`: AI response token limit (50-2000)

#### ❌ NOT Built Yet:
- Subscription/billing system (Stripe, payment processing)
- Usage tracking/metering for limits enforcement
- Auto-downgrade/upgrade based on subscription status
- Pricing tier UI/selection during signup
- Monthly conversation limit enforcement
- Knowledge MB limit enforcement

---

## 3. Tenant Capabilities System

### Database Schema

```python
class TenantCapabilities(Base):
    """Feature flags and usage limits for each tenant."""

    tenant_id: UUID  # One-to-one with tenant

    # Feature Permissions
    can_create_tickets: bool = True       # Escalate to support tickets
    can_access_analytics: bool = True     # View analytics dashboard
    can_upload_knowledge: bool = True     # Upload knowledge documents

    # Resource Limits
    max_knowledge_mb: int = 100           # Max storage (MB)
    max_conversations_per_month: int = None  # Null = unlimited
```

### Default Values

When a new tenant is created, they receive:

```python
{
    "can_create_tickets": True,
    "can_access_analytics": True,
    "can_upload_knowledge": True,
    "max_knowledge_mb": 10,              # 10 MB default
    "max_conversations_per_month": None   # Unlimited
}
```

Source: `app/services/tenant_service.py:85-88`

---

## 4. AI Configuration Limits

### Database Schema

```python
class TenantAIConfig(Base):
    """AI behavior configuration for each tenant."""

    tenant_id: UUID  # One-to-one with tenant

    # Response Generation
    max_response_tokens: int = 200         # Default: 200 tokens
    confidence_threshold: float = 0.55     # Min confidence (0-1)
    grounding_requirement: bool = True     # Must use KB

    # Brand Voice & Behavior
    brand_voice: dict = {}                 # {tone, formality, etc.}
    allowed_topics: list = None            # Null = all allowed
    blocked_topics: list = []              # Topics to avoid
    escalation_keywords: list = []         # Auto-escalate triggers
    custom_instructions: str = None        # Custom prompt
```

### Schema Validation Limits

From `app/schemas/tenant.py:40`:

```python
max_response_tokens: int = Field(
    default=800,        # Default raised from 200 to 800
    ge=50,              # Minimum: 50 tokens
    le=2000,            # Maximum: 2000 tokens (hard cap)
)
```

**Note:** Database model defaults to 200, but schema defaults to 800. Seed data uses 400. *NB*: we should fix this to use the corrected as we have it in the docs readme, from line 442

---

## 5. Feature Access Control

### Currently Enforced

#### 1. Analytics Access (`can_access_analytics`)

**Endpoint:** `GET /api/v1/analytics/*`
**Enforcement:** `app/api/v1/endpoints/analytics.py:48`

```python
if not tenant.capabilities or not tenant.capabilities.can_access_analytics:
    raise HTTPException(
        status_code=403,
        detail="Analytics access not enabled for this tenant"
    )
```

**Verified in:** Tests confirm this is enforced

#### 2. Ticket Creation (`can_create_tickets`)

**Current Status:** ⚠️ **NOT ENFORCED**
- Flag exists in database
- No enforcement in `app/services/ticket_service.py`
- Tickets can be created even if flag is False

#### 3. Knowledge Upload (`can_upload_knowledge`)

**Current Status:** ⚠️ **NOT ENFORCED**
- Flag exists in database
- No enforcement in `app/api/v1/endpoints/knowledge_upload.py`
- Documents can be uploaded even if flag is False

---

## 6. Resource Limits

### Knowledge Storage (`max_knowledge_mb`)

**Current Status:** ⚠️ **NOT ENFORCED**

- Limit is stored: Default 100 MB per tenant
- No validation in upload endpoints
- No tracking of current storage usage
- Tenants can exceed limit

**To implement:**
```python
# Calculate current usage
current_mb = sum(source.file_size_mb for source in tenant.knowledge_sources)

if current_mb + new_file_size_mb > tenant.capabilities.max_knowledge_mb:
    raise HTTPException(403, "Knowledge storage limit exceeded")
```

### Monthly Conversations (`max_conversations_per_month`)

**Current Status:** ⚠️ **NOT ENFORCED**

- Limit is stored: Default `None` (unlimited)
- No tracking of monthly conversation count
- No enforcement before creating sessions

**To implement:**
```python
# Count this month's conversations
count = db.query(ConversationSession).filter(
    ConversationSession.tenant_id == tenant_id,
    ConversationSession.created_at >= start_of_month
).count()

if tenant.capabilities.max_conversations_per_month is not None:
    if count >= tenant.capabilities.max_conversations_per_month:
        raise HTTPException(403, "Monthly conversation limit reached")
```

### AI Response Tokens (`max_response_tokens`)

**Current Status:** ✅ **ENFORCED**

- Used in `app/services/rag_service.py` for AI generation
- Passed to Google Gemini API as `max_tokens` parameter
- Hard cap at 2000 tokens (schema validation)

---

## 7. Default Configuration

### New Tenant Defaults

When created via `POST /api/v1/platform/tenants` or company registration:

```json
{
  "ai_config": {
    "max_response_tokens": 200,
    "confidence_threshold": 0.55,  // I think this was hardcoded already, didn't it? anyways
    "grounding_requirement": true,
    "brand_voice": {},
    "allowed_topics": null,
    "blocked_topics": [],
    "escalation_keywords": [],
    "custom_instructions": null
  },
  "capabilities": {
    "can_create_tickets": true,
    "can_access_analytics": true,
    "can_upload_knowledge": true,
    "max_knowledge_mb": 100,
    "max_conversations_per_month": null
  }
}
```

### Seed Data Configuration

Test tenant (Weysure) is configured with:

```python
max_response_tokens = 400  # More generous than default
max_knowledge_mb = 100     # Standard
max_conversations_per_month = None  # Unlimited
```

Source: `scripts/seed_data.py:216`

---

## 8. API Endpoints

### For Platform Admins

#### View Tenant Capabilities
```
GET /api/v1/platform/tenants/{tenant_id}
Authorization: Bearer {platform_admin_token}

Response includes:
{
  "capabilities": {
    "can_create_tickets": true,
    "can_access_analytics": true,
    "can_upload_knowledge": true,
    "max_knowledge_mb": 100,
    "max_conversations_per_month": null
  }
}
```

#### Update Tenant Capabilities
```
PATCH /api/v1/platform/tenants/{tenant_id}/capabilities
Authorization: Bearer {platform_admin_token}

Body:
{
  "can_upload_knowledge": false,    // Disable for non-payment
  "max_knowledge_mb": 500,          // Upgrade to 500 MB
  "max_conversations_per_month": 10000  // Set limit
}
```

### For Tenant Users

#### View Own Capabilities (Read-Only)
```
GET /api/v1/tenant/capabilities
Authorization: Bearer {tenant_user_token}

Response:
{
  "can_create_tickets": true,
  "can_access_analytics": true,
  "can_upload_knowledge": true,
  "max_knowledge_mb": 100,
  "max_conversations_per_month": null
}
```

**Note:** Tenants can VIEW but NOT UPDATE their own capabilities.

---

## 9. Enforcement Points

### Summary Table

| Feature | Capability Flag | Enforced? | Location |
|---------|----------------|-----------|----------|
| **Analytics Dashboard** | `can_access_analytics` | ✅ Yes | `app/api/v1/endpoints/analytics.py:48` |
| **Ticket Creation** | `can_create_tickets` | ❌ No | Not implemented |
| **Knowledge Upload** | `can_upload_knowledge` | ❌ No | Not implemented |
| **Knowledge Storage Limit** | `max_knowledge_mb` | ❌ No | Not tracked/enforced |
| **Monthly Conversations** | `max_conversations_per_month` | ❌ No | Not tracked/enforced |
| **AI Response Tokens** | `max_response_tokens` | ✅ Yes | Passed to Gemini API |

### Enforcement Pattern

```python
# Analytics enforcement example (IMPLEMENTED)
def check_analytics_access(tenant: Tenant):
    if not tenant.capabilities or not tenant.capabilities.can_access_analytics:
        raise HTTPException(403, "Analytics access not enabled")

# Recommended pattern for other features
def check_feature_access(tenant: Tenant, feature: str):
    capability_field = f"can_{feature}"
    if not getattr(tenant.capabilities, capability_field, False):
        raise HTTPException(403, f"{feature.title()} access not enabled")
```

---

## 10. Recommended Pricing Tiers

Based on actual implemented capabilities and industry standards, here are recommended pricing tiers:

### Free Tier
**Price:** ₦0/month

**Capabilities:**
```json
{
  "can_create_tickets": true,
  "can_access_analytics": false,    // ❌ No analytics
  "can_upload_knowledge": true,
  "max_knowledge_mb": 10,            // Limited storage
  "max_knowledge_upload": 2,         // Maximum 2 documents
  "max_conversations_per_month": 100,
  "max_response_tokens": 800
}
```

**Included:**
- 100 conversations/month
- 10 MB knowledge base (max 2 documents)
- Ticket escalation
- Email support
- Basic chat widget

**Limitations:**
- No analytics dashboard
- Community support only
- Limited to 2 knowledge documents

---

### Starter Tier
**Price:** ₦15,000/month (~$18 USD)

**Capabilities:**
```json
{
  "can_create_tickets": true,
  "can_access_analytics": true,     // ✅ Analytics enabled
  "can_upload_knowledge": true,
  "max_knowledge_mb": 100,
  "max_knowledge_upload": 10,        // Maximum 10 documents
  "max_conversations_per_month": 1000,
  "max_response_tokens": 1000
}
```

**Included:**
- 1,000 conversations/month
- 100 MB knowledge base (max 10 documents)
- Full analytics dashboard
- Ticket management
- Custom branding (widget colors, logo)
- Email support
- Up to 3 team members

---

### Professional Tier
**Price:** ₦35,000/month (~$42 USD)

**Capabilities:**
```json
{
  "can_create_tickets": true,
  "can_access_analytics": true,
  "can_upload_knowledge": true,
  "max_knowledge_mb": 500,
  "max_knowledge_upload": 50,        // Maximum 50 documents
  "max_conversations_per_month": 10000,
  "max_response_tokens": 1500
}
```

**Included:**
- 10,000 conversations/month
- 500 MB knowledge base (max 50 documents)
- Advanced analytics
- API access
- Priority support
- Up to 10 team members
- WebSocket real-time chat
- Custom AI instructions

---

### Enterprise Tier
**Price:** Custom pricing (starting ₦100,000/month)

**Capabilities:**
```json
{
  "can_create_tickets": true,
  "can_access_analytics": true,
  "can_upload_knowledge": true,
  "max_knowledge_mb": null,          // Unlimited
  "max_knowledge_upload": null,      // Unlimited documents
  "max_conversations_per_month": null,  // Unlimited
  "max_response_tokens": 2000
}
```

**Included:**
- Unlimited conversations
- Unlimited knowledge storage
- Unlimited documents
- Dedicated support
- SLA guarantees (99.9% uptime)
- White-label options
- Custom integrations
- Unlimited team members
- Advanced customization
- Dedicated account manager

---

## 11. Implementation Gaps & Future Work

### High Priority

#### 1. Add `max_knowledge_upload` Capability
**Impact:** Limit number of documents per tier (better than just MB limit)

**Database Migration:**
```python
# alembic/versions/xxx_add_max_knowledge_upload.py
def upgrade():
    op.add_column('tenant_capabilities',
        sa.Column('max_knowledge_upload', sa.Integer(), nullable=True,
                  comment='Maximum number of knowledge documents (null = unlimited)')
    )

    # Set defaults for existing tenants
    op.execute("UPDATE tenant_capabilities SET max_knowledge_upload = 10")
```

**Model Update:**
```python
# app/db/models/tenant.py
class TenantCapabilities(Base):
    # Add this field
    max_knowledge_upload: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum number of knowledge documents (null = unlimited)",
    )
```

**Enforcement:**
```python
# In app/api/v1/endpoints/knowledge_upload.py
@router.post("/upload")
async def upload_knowledge(...):
    tenant = get_tenant(tenant_id)

    # Check document count limit
    if tenant.capabilities.max_knowledge_upload is not None:
        current_count = count_active_knowledge_sources(tenant_id)
        if current_count >= tenant.capabilities.max_knowledge_upload:
            raise HTTPException(
                403,
                f"Document limit reached ({current_count}/{tenant.capabilities.max_knowledge_upload}). "
                f"Upgrade to Professional for 50 documents or Enterprise for unlimited."
            )
```

#### 2. Enforce Knowledge Upload Permission
**Impact:** Prevent unauthorized uploads if subscription lapses

```python
# In app/api/v1/endpoints/knowledge_upload.py
@router.post("/upload")
async def upload_knowledge(...):
    tenant = get_tenant(tenant_id)

    # ADD THIS CHECK
    if not tenant.capabilities.can_upload_knowledge:
        raise HTTPException(403, "Knowledge upload not enabled for your plan")
```

#### 2. Enforce Knowledge Storage Limit
**Impact:** Prevent storage abuse, align with pricing tiers

```python
# Before upload
current_usage_mb = calculate_tenant_storage(tenant_id)
if current_usage_mb + file_size_mb > tenant.capabilities.max_knowledge_mb:
    raise HTTPException(403, f"Storage limit exceeded. Upgrade to Professional plan for 500 MB")
```

#### 3. Track & Enforce Monthly Conversation Limit
**Impact:** Align usage with paid limits

```python
# Before creating session
monthly_count = count_monthly_conversations(tenant_id)
limit = tenant.capabilities.max_conversations_per_month

if limit is not None and monthly_count >= limit:
    raise HTTPException(
        403,
        f"Monthly limit of {limit} conversations reached. Upgrade to continue."
    )
```

#### 4. Enforce Ticket Creation Permission
**Impact:** Gate premium features

```python
# In app/services/ticket_service.py
if not tenant.capabilities.can_create_tickets:
    # Still save message but don't create ticket
    return {"escalated": False, "reason": "Ticket creation disabled"}
```

### Medium Priority

#### 5. Add Subscription/Billing System
- Integrate Stripe or similar payment processor
- Create `Subscription` model with status tracking
- Auto-update capabilities based on subscription status
- Handle payment failures, cancellations

#### 6. Add Usage Dashboard for Admins
- Current month's conversation count
- Knowledge storage usage (MB)
- Percentage of limits used
- Upgrade prompts when nearing limits

#### 7. Add Team Member Limits
Create `max_team_members` capability:

```python
# TenantCapabilities
max_team_members: int = 1  # Free tier: 1 member

# Enforce in user creation
user_count = count_tenant_users(tenant_id)
if user_count >= tenant.capabilities.max_team_members:
    raise HTTPException(403, "Team member limit reached")
```

### Low Priority

#### 8. Rate Limiting per Tier
- Different API rate limits per tier
- Priority queue for Enterprise customers

#### 9. Feature Flags for Advanced Features
- `can_use_api`: API access for Professional+
- `can_use_websockets`: Real-time for Professional+
- `can_customize_widget`: Branding for Starter+
- `has_white_label`: Remove "Powered by Luran" for Enterprise

#### 10. Analytics Depth by Tier
- Free: No analytics
- Starter: Basic metrics (7 days)
- Professional: Advanced analytics (90 days)
- Enterprise: Full historical data

---

## Appendix: Database Queries

### Check Tenant Capabilities
```sql
SELECT
    t.name,
    t.display_name,
    tc.can_create_tickets,
    tc.can_access_analytics,
    tc.can_upload_knowledge,
    tc.max_knowledge_mb,
    tc.max_conversations_per_month,
    tai.max_response_tokens
FROM tenants t
JOIN tenant_capabilities tc ON t.id = tc.tenant_id
JOIN tenant_ai_configs tai ON t.id = tai.tenant_id
WHERE t.name = 'weysure';
```

### Update Capabilities (Platform Admin)
```sql
UPDATE tenant_capabilities
SET
    max_knowledge_mb = 500,
    max_conversations_per_month = 10000,
    can_access_analytics = true
WHERE tenant_id = 'your-tenant-uuid';
```

### Count Monthly Conversations
```sql
SELECT COUNT(*)
FROM conversation_sessions
WHERE tenant_id = 'your-tenant-uuid'
  AND created_at >= date_trunc('month', CURRENT_DATE);
```

### Calculate Knowledge Storage Usage
```sql
SELECT
    tenant_id,
    SUM(file_size_bytes) / 1024.0 / 1024.0 AS usage_mb
FROM knowledge_sources
WHERE status = 'ACTIVE'
  AND tenant_id = 'your-tenant-uuid'
GROUP BY tenant_id;
```

---

## Summary

**What Works Today:**
- ✅ Capability system exists and is flexible
- ✅ Platform admins can configure per-tenant limits
- ✅ Analytics access is enforced
- ✅ AI token limits work

**What Needs Work:**
- ❌ Most limits are not enforced (knowledge, conversations, tickets)
- ❌ No usage tracking/metering
- ❌ No billing/subscription system
- ❌ No auto-upgrade/downgrade logic

**Recommendation:**
Implement enforcement for existing capabilities before adding new features. The infrastructure is solid; it just needs enforcement logic at the service layer.

---

**Document Version:** 1.0
**Author:** Based on codebase analysis (2026-01-25)
**Next Review:** After implementing enforcement (Phase 2)
