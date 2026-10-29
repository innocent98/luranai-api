# Luran Subscription & Pricing Model Documentation

> **What counts as a conversation?** A conversation is one continuous exchange between a customer and your AI or team, from their first message until they have been quiet for 6 hours. It is not counted per message: one conversation can hold any number of messages, images and replies. If the same customer comes back within 6 hours it is still the same conversation; after 6 hours it is a new one. A conversation handed to a human agent stays one conversation until it is resolved. Conversations are counted per calendar month (UTC) and the count resets on the 1st. WhatsApp follows the same rule per chat.

> **Historical document.** Prices and limits here predate the 2026-10-28 pricing and entitlements change (Lite is now 15,000 naira/month, with plan gates). The current definition is `app/services/plan_catalog.py` and `docs/SUBSCRIPTION_PLANS.md`.

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

## 10. Pricing Tiers (Final)

Based on actual implementation, AI costs (Gemini 2.5 Flash-Lite), and Nigerian market analysis.

**Key Decisions:**
- ✅ **Free Tier:** 30-day trial only (not permanent free tier)
- ✅ **Combined Knowledge Sources:** Files + URLs counted together (not separate limits)
- ✅ **Annual Billing:** 2 months free (10 months price for 12 months service)
- ✅ **AI Model:** Gemini 2.5 Flash-Lite (₦1.05/conversation)

---

### Free Trial (30 Days)
**Price:** ₦0 for 30 days after account creation

**Duration:** Automatically starts on company registration, expires after 30 days

**Capabilities:**
```json
{
  "can_create_tickets": true,
  "can_access_analytics": false,    // ❌ No analytics
  "can_upload_knowledge": true,

  // Knowledge sources
  "max_knowledge_sources": 2,        // Total: 2 files OR 2 URLs OR 1 file + 1 URL
  "can_use_url_sources": false,      // ❌ File uploads only (no URL scraping)
  "max_knowledge_mb": 10,

  // Usage limits
  "max_conversations_per_month": 100,
  "max_response_tokens": 800,
  "max_team_members": 1,

  // Trial tracking
  "trial_ends_at": "2026-02-25T00:00:00Z",  // 30 days from signup
  "subscription_status": "trialing"
}
```

**Included:**
- **30-day trial period**
- 100 conversations/month
- 2 knowledge sources (files only, no URLs)
- 10 MB total storage
- Ticket escalation
- Basic chat widget
- 1 team member (account owner)
- Community support

**Limitations:**
- ❌ No analytics dashboard
- ❌ No URL source scraping
- ❌ Trial expires after 30 days
- ❌ Must upgrade to paid plan to continue using Luran

**After Trial Expires:**
- Account status: `trial_expired`
- Widget stops working (returns upgrade message)
- Knowledge base becomes read-only
- Must upgrade to Lite, Professional, or Enterprise to reactivate

---

### Lite Tier
**Price:**
- **Monthly:** ₦12,000/month
- **Annual:** ₦120,000/year (₦10,000/month - **save ₦24,000/year!** 🎉)

**Capabilities:**
```json
{
  "can_create_tickets": true,
  "can_access_analytics": true,     // ✅ Analytics enabled
  "can_upload_knowledge": true,

  // Knowledge sources (combined files + URLs)
  "max_knowledge_sources": 10,       // Total: any combination of files + URLs
  "can_use_url_sources": true,       // ✅ URL scraping enabled
  "max_knowledge_mb": 50,

  // Usage limits
  "max_conversations_per_month": 1000,
  "max_response_tokens": 1000,
  "max_team_members": 3,

  // Subscription
  "subscription_status": "active",
  "billing_cycle": "monthly" // or "annual"
}
```

**Included:**
- 1,000 conversations/month
- **10 knowledge sources** (files + URLs combined)
  - Examples: 8 files + 2 URLs, or 5 files + 5 URLs, or 10 URLs only
- 50 MB total storage
- **✅ URL source scraping** (scrape FAQ pages, About pages, etc.)
- **✅ Full analytics dashboard**
- Ticket management
- Custom branding (widget colors, logo)
- Up to 3 team members
- Email support (24-48 hour response)

**Use Cases:**
- Small businesses & startups
- 20-50 customer inquiries/day
- Basic knowledge base (product FAQs, policies)

**Annual Savings:** Pay ₦120,000 instead of ₦144,000 (save 2 months)

---

### Professional Tier
**Price:**
- **Monthly:** ₦35,000/month
- **Annual:** ₦350,000/year (₦29,167/month - **save ₦70,000/year!** 🎉)

**Capabilities:**
```json
{
  "can_create_tickets": true,
  "can_access_analytics": true,
  "can_upload_knowledge": true,

  // Knowledge sources (combined files + URLs)
  "max_knowledge_sources": 30,       // Total: any combination of files + URLs
  "can_use_url_sources": true,
  "max_knowledge_mb": 200,

  // Usage limits
  "max_conversations_per_month": 5000,
  "max_response_tokens": 1500,
  "max_team_members": 10,

  // Advanced features
  "can_use_api": true,               // API access
  "can_use_websockets": true,        // Real-time chat

  // Subscription
  "subscription_status": "active",
  "billing_cycle": "monthly" // or "annual"
}
```

**Included:**
- 5,000 conversations/month
- **30 knowledge sources** (files + URLs combined)
  - Example: Entire documentation site + product manuals + internal guides
- 200 MB total storage
- URL source scraping (scrape entire knowledge base sections)
- **Advanced analytics** (90-day history, export reports)
- **API access** (integrate with your systems)
- **Priority support** (4-8 hour response, weekdays)
- Up to 10 team members
- WebSocket real-time chat
- Custom AI instructions
- Custom branding

**Use Cases:**
- Growing businesses
- E-commerce sites with 100-200 inquiries/day
- Multiple product lines
- Integration with CRM/ticketing systems

**Annual Savings:** Pay ₦350,000 instead of ₦420,000 (save 2 months)

---

### Enterprise Tier
**Price:** Custom pricing (starting ₦100,000/month)

**Minimum Contract:** Annual billing required

**Capabilities:**
```json
{
  "can_create_tickets": true,
  "can_access_analytics": true,
  "can_upload_knowledge": true,

  // Knowledge sources
  "max_knowledge_sources": null,     // ♾️ Unlimited
  "can_use_url_sources": true,
  "max_knowledge_mb": null,          // ♾️ Unlimited

  // Usage limits
  "max_conversations_per_month": null,  // ♾️ Unlimited
  "max_response_tokens": 2000,
  "max_team_members": null,          // ♾️ Unlimited

  // Enterprise features
  "can_use_api": true,
  "can_use_websockets": true,
  "has_white_label": true,           // Remove "Powered by Luran"
  "has_sla": true,                   // 99.9% uptime guarantee
  "has_dedicated_support": true,

  // Subscription
  "subscription_status": "active",
  "billing_cycle": "annual"
}
```

**Included:**
- **Unlimited conversations**
- **Unlimited knowledge sources** (files + URLs)
- **Unlimited storage**
- **Unlimited team members**
- Dedicated account manager
- **SLA guarantees** (99.9% uptime)
- **White-label options** (remove Luran branding)
- Custom integrations
- Advanced customization
- Dedicated support (1-2 hour response, 24/7)
- Onboarding & training
- Custom contract terms

**Use Cases:**
- Large enterprises
- High-volume customer support (500+ inquiries/day)
- Multi-brand businesses
- Custom compliance requirements

**Pricing:** Contact sales for custom quote based on volume and requirements

---

### Pricing Comparison Table

| Feature | Free Trial | Lite | Professional | Enterprise |
|---------|-----------|------|--------------|------------|
| **Monthly Price** | ₦0 (30 days) | ₦12,000 | ₦35,000 | ₦100,000+ |
| **Annual Price** | N/A | ₦120,000 | ₦350,000 | Custom |
| **Annual Savings** | N/A | ₦24,000 | ₦70,000 | Negotiable |
| **Trial Duration** | 30 days | N/A | N/A | N/A |
| | | | | |
| **Conversations/month** | 100 | 1,000 | 5,000 | Unlimited |
| **Knowledge sources** | 2 (files only) | 10 (files+URLs) | 30 (files+URLs) | Unlimited |
| **Storage (MB)** | 10 | 50 | 200 | Unlimited |
| **URL scraping** | ❌ | ✅ | ✅ | ✅ |
| **Team members** | 1 | 3 | 10 | Unlimited |
| **AI tokens/response** | 800 | 1,000 | 1,500 | 2,000 |
| | | | | |
| **Analytics** | ❌ | ✅ Basic | ✅ Advanced | ✅ Custom |
| **API access** | ❌ | ❌ | ✅ | ✅ |
| **WebSockets** | ❌ | ❌ | ✅ | ✅ |
| **Custom branding** | ❌ | ✅ Colors/logo | ✅ Colors/logo | ✅ White-label |
| **Support** | Community | Email 24-48h | Priority 4-8h | Dedicated 1-2h |
| **SLA** | ❌ | ❌ | ❌ | 99.9% uptime |
| **Account manager** | ❌ | ❌ | ❌ | ✅ Dedicated |

---

### Cost Analysis (Gemini 2.5 Flash-Lite)

**AI Cost per conversation:** ₦1.05 (1000 input + 1500 output tokens)

| Tier | Conversations | AI Cost/mo | Revenue | Gross Margin |
|------|--------------|------------|---------|--------------|
| **Free Trial** | 100 | ₦105 | ₦0 | Loss (acquisition cost) |
| **Lite** | 1,000 | ₦1,050 | ₦12,000 | 91% |
| **Professional** | 5,000 | ₦5,250 | ₦35,000 | 85% |
| **Enterprise** | 10,000+ | ₦10,500+ | ₦100,000+ | 89%+ |

**Note:** Infrastructure costs (~₦2,000-4,000 per customer at scale) not included above.

**Total margin:** 75-85% after infrastructure costs = healthy SaaS metrics ✅

---

## 11. Implementation Requirements

### Phase 1: Database Schema Updates (HIGH PRIORITY)

#### 1.1 Add Subscription & Trial Tracking

**New fields for `tenants` table:**
```python
# app/db/models/tenant.py
class Tenant(Base):
    # Existing fields...

    # NEW: Subscription tracking
    subscription_tier: Mapped[str] = mapped_column(
        String(50),
        default="trial",
        nullable=False,
        comment="Subscription tier: trial, lite, professional, enterprise",
    )
    subscription_status: Mapped[str] = mapped_column(
        String(50),
        default="trialing",
        nullable=False,
        comment="Status: trialing, active, past_due, canceled, trial_expired",
    )
    billing_cycle: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        comment="Billing cycle: monthly, annual (null for trial/free)",
    )
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When trial period ends (30 days from signup)",
    )
    subscription_starts_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When paid subscription started",
    )
    subscription_ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When subscription ends (for canceled or expired)",
    )
```

#### 1.2 Add Combined Knowledge Sources Limit

**Update `tenant_capabilities` table:**
```python
# app/db/models/tenant.py
class TenantCapabilities(Base):
    # Existing fields...

    # NEW: Combined knowledge sources (replaces separate file/URL limits)
    max_knowledge_sources: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Total knowledge sources: files + URLs combined (null = unlimited)",
    )

    # NEW: URL source control
    can_use_url_sources: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Can scrape webpage URLs as knowledge sources",
    )

    # NEW: Team member limits
    max_team_members: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum team members allowed (null = unlimited)",
    )
```

#### 1.3 Add Source Type Tracking

**Update `knowledge_sources` table:**
```python
# app/db/models/knowledge.py
class KnowledgeSource(Base):
    # Add source_type to distinguish files vs URLs
    source_type: Mapped[str] = mapped_column(
        String(20),
        default="file",
        nullable=False,
        comment="Source type: file, url",
    )

    # For URL sources
    source_url: Mapped[Optional[str]] = mapped_column(
        String(2048),
        nullable=True,
        comment="Original URL for url-type sources",
    )
```

---

### Phase 2: Trial Period Enforcement (HIGH PRIORITY)

#### 2.1 Auto-Set Trial Period on Signup

**Update registration logic:**
```python
# app/services/tenant_service.py
def create_tenant(self, tenant_data: TenantCreate) -> Tenant:
    # Calculate trial end date (30 days from now)
    trial_ends_at = datetime.utcnow() + timedelta(days=30)

    db_tenant = Tenant(
        name=tenant_data.name,
        subscription_tier="trial",
        subscription_status="trialing",
        trial_ends_at=trial_ends_at,
        # ... other fields
    )

    # Set trial capabilities
    capabilities = TenantCapabilities(
        tenant_id=db_tenant.id,
        can_access_analytics=False,  # No analytics in trial
        max_knowledge_sources=2,
        can_use_url_sources=False,   # Files only in trial
        max_knowledge_mb=10,
        max_conversations_per_month=100,
        max_team_members=1,
    )
```

#### 2.2 Trial Expiration Check Middleware

**Create middleware to check trial status:**
```python
# app/api/middleware/subscription.py
async def check_subscription_status(request: Request, call_next):
    """Check if tenant subscription is valid before processing request."""

    # Skip for certain endpoints (login, register, etc.)
    if request.url.path in EXEMPT_PATHS:
        return await call_next(request)

    tenant = get_current_tenant(request)

    # Check trial expiration
    if tenant.subscription_status == "trialing":
        if tenant.trial_ends_at and datetime.utcnow() > tenant.trial_ends_at:
            tenant.subscription_status = "trial_expired"
            db.commit()

            raise HTTPException(
                status_code=402,  # Payment Required
                detail={
                    "error": "trial_expired",
                    "message": "Your 30-day trial has expired. Please upgrade to continue using Luran.",
                    "trial_ended": tenant.trial_ends_at,
                    "upgrade_url": "/upgrade"
                }
            )

    # Check other statuses
    if tenant.subscription_status in ["trial_expired", "canceled", "past_due"]:
        raise HTTPException(402, "Subscription required")

    return await call_next(request)
```

---

### Phase 3: Knowledge Source Enforcement (HIGH PRIORITY)

#### 3.1 Enforce Combined Source Limit

**Before file upload or URL scraping:**
```python
# In app/api/v1/endpoints/knowledge_upload.py
@router.post("/upload")
async def upload_knowledge(...):
    tenant = get_tenant(tenant_id)

    # Check if uploads are allowed
    if not tenant.capabilities.can_upload_knowledge:
        raise HTTPException(403, "Knowledge upload disabled for your subscription")

    # Count existing sources (files + URLs combined)
    total_sources = db.query(KnowledgeSource).filter(
        KnowledgeSource.tenant_id == tenant_id,
        KnowledgeSource.status == SourceStatus.ACTIVE
    ).count()

    # Check combined limit
    if tenant.capabilities.max_knowledge_sources is not None:
        if total_sources >= tenant.capabilities.max_knowledge_sources:
            limit = tenant.capabilities.max_knowledge_sources
            raise HTTPException(
                403,
                f"Knowledge source limit reached ({total_sources}/{limit}). "
                f"Upgrade to Professional for 30 sources or Enterprise for unlimited."
            )

    # For URL sources specifically, check if enabled
    if source_type == "url":
        if not tenant.capabilities.can_use_url_sources:
            raise HTTPException(
                403,
                "URL sources not enabled for trial. "
                "Upgrade to Lite plan for URL scraping."
            )
```

#### 3.2 Enforce Storage Limit

**Track total MB usage:**
```python
# Calculate current storage
current_mb = db.query(func.sum(KnowledgeSource.file_size_bytes)).filter(
    KnowledgeSource.tenant_id == tenant_id,
    KnowledgeSource.status == SourceStatus.ACTIVE
).scalar() or 0
current_mb = current_mb / 1024 / 1024

# Check limit
if current_mb + new_file_mb > tenant.capabilities.max_knowledge_mb:
    raise HTTPException(
        403,
        f"Storage limit exceeded ({current_mb:.1f}/{tenant.capabilities.max_knowledge_mb} MB). "
        f"Upgrade for more storage."
    )
```

---

### Phase 4: Usage Enforcement (HIGH PRIORITY)

#### 4.1 Track & Enforce Monthly Conversation Limit

```python
# Before creating session in app/api/v1/endpoints/chat.py
from datetime import datetime
from dateutil.relativedelta import relativedelta

# Get start of current month
month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

# Count conversations this month
monthly_count = db.query(ConversationSession).filter(
    ConversationSession.tenant_id == tenant_id,
    ConversationSession.created_at >= month_start
).count()

# Check limit
limit = tenant.capabilities.max_conversations_per_month
if limit is not None and monthly_count >= limit:
    raise HTTPException(
        402,  # Payment Required
        detail={
            "error": "conversation_limit_reached",
            "message": f"Monthly limit of {limit} conversations reached.",
            "current_count": monthly_count,
            "upgrade_url": "/upgrade"
        }
    )
```

#### 4.2 Team Member Limit Enforcement

```python
# Before creating user in app/services/auth_service.py
active_users = db.query(User).filter(
    User.tenant_id == tenant_id,
    User.is_active == True
).count()

limit = tenant.capabilities.max_team_members
if limit is not None and active_users >= limit:
    raise HTTPException(
        403,
        f"Team member limit reached ({active_users}/{limit}). "
        f"Upgrade to add more team members."
    )
```

---

### Phase 5: Billing Integration (MEDIUM PRIORITY)

#### 5.1 Stripe Integration

**Required endpoints:**
- `POST /api/v1/billing/checkout` - Create Stripe checkout session
- `POST /api/v1/billing/portal` - Customer portal for managing subscription
- `POST /api/v1/billing/webhook` - Handle Stripe webhooks

**Webhook events to handle:**
- `checkout.session.completed` - Activate subscription
- `invoice.paid` - Renew subscription
- `invoice.payment_failed` - Mark as past_due
- `customer.subscription.deleted` - Cancel subscription

#### 5.2 Subscription Management Service

```python
# app/services/subscription_service.py
class SubscriptionService:
    def upgrade_subscription(self, tenant_id, tier, billing_cycle):
        """Upgrade tenant to paid tier."""
        # Update tenant tier and status
        # Update capabilities based on tier
        # Create Stripe subscription
        pass

    def downgrade_subscription(self, tenant_id):
        """Downgrade/cancel subscription."""
        # Mark for cancellation at period end
        pass

    def handle_trial_upgrade(self, tenant_id, tier, billing_cycle):
        """Convert trial to paid subscription."""
        # Set subscription_status = "active"
        # Clear trial_ends_at
        # Set subscription_starts_at
        pass
```

---

### Phase 6: URL Source Scraping (MEDIUM PRIORITY)

**New endpoint:**
```python
# POST /api/v1/knowledge/url
async def add_url_source(
    url: HttpUrl,
    title: str,
    tenant_id: UUID,
):
    """Scrape webpage and add as knowledge source."""

    # Check if URL sources enabled
    if not tenant.capabilities.can_use_url_sources:
        raise HTTPException(403, "URL sources not enabled. Upgrade to Lite.")

    # Check source limit
    check_knowledge_source_limit(tenant_id, source_type="url")

    # Scrape URL (use BeautifulSoup or similar)
    content = scrape_webpage(url)

    # Create knowledge source
    source = KnowledgeSource(
        tenant_id=tenant_id,
        source_type="url",
        source_url=str(url),
        title=title,
        content=content,
    )

    # Process and embed
    # (similar to file upload flow)
```

---

### Implementation Priority Summary

| Phase | Priority | Estimated Effort | Impact |
|-------|----------|------------------|--------|
| **Phase 1:** Database Schema | 🔴 HIGH | 2-3 days | Foundation for everything |
| **Phase 2:** Trial Enforcement | 🔴 HIGH | 1-2 days | Core business model |
| **Phase 3:** Knowledge Limits | 🔴 HIGH | 1-2 days | Prevent abuse |
| **Phase 4:** Usage Limits | 🔴 HIGH | 1-2 days | Enforce tier boundaries |
| **Phase 5:** Billing (Stripe) | 🟡 MEDIUM | 3-5 days | Enable revenue |
| **Phase 6:** URL Scraping | 🟡 MEDIUM | 2-3 days | Feature differentiation |

**Total Estimated Time:** 10-17 days for full implementation

**MVP (Phases 1-4 only):** 5-9 days

---

### Low Priority (Future Enhancements)

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
