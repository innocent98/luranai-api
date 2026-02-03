# Frontend Development Guide - Complete API Reference

**Last Updated:** 2026-01-27
**Base URL:** `http://localhost:8000/api/v1` (development) | `https://api.luranai.com/api/v1` (production)

## Table of Contents

1. [Authentication Overview](#authentication-overview)
2. [Platform Admin Dashboard](#platform-admin-dashboard)
   - Platform Admin Login
   - Get Current Platform Admin Info
   - Change Platform Admin Password
   - Create New Platform Admin
   - List All Platform Admins
   - **List All Tenants (Platform Admin)**
   - **Get Tenant Details (Platform Admin)**
   - **Deactivate Tenant (Platform Admin)**
   - **Update Tenant Capabilities (Platform Admin)**
3. [Company/Tenant Dashboard](#companytenant-dashboard)
   - Company Registration, Login, Email Verification
   - Company Settings & Information
   - AI Chatbot Configuration
   - Subscription Features & Limits
4. [Team Management](#team-management)
5. [API Key Management](#api-key-management)
6. [Widget Configuration](#widget-configuration)
7. [Knowledge Management](#knowledge-management)
8. [Agent Support Dashboard](#agent-support-dashboard)
   - List Escalated Conversations
   - View Conversation Details
   - Get Message History
   - Agent Takeover
   - Send Agent Message
   - WebSocket Real-Time Chat (Agent)
   - WebSocket Real-Time Chat (Customer)
9. [Ticket Management](#ticket-management)
   - List Tickets
   - Get Ticket Details
   - Create Ticket Manually
   - Assign Ticket
   - Update Ticket
   - Resolve Ticket
   - Close Ticket
   - Get Unassigned Tickets
10. [Analytics Dashboard](#analytics-dashboard)
   - Overview Metrics
   - Conversation Analytics
   - Ticket Analytics
   - Agent Performance (All Agents)
   - Individual Agent Performance
   - Conversation Trends
   - Ticket Trends
   - Knowledge Usage Analytics
11. [Review & Rating System](#review--rating-system)
   - Submit Session Rating (Public)
   - List Session Ratings
   - Submit Platform Feedback
   - List Platform Feedback
   - Submit Public Feedback (Landing Page)
   - List Public Feedback
12. [Subscription & Payment System](#subscription--payment-system)
   - Overview & Pricing Tiers
   - Initialize Subscription Payment
   - Verify Subscription Payment
   - Direct Subscription Upgrade (Deprecated)
   - Subscription Enforcement (HTTP 402 Errors)
   - Webhooks (Backend Reference)
   - Payment Flow Diagram
   - Recurring Billing
   - Frontend Implementation Example
   - Testing Checklist
   - Common Issues & Solutions
13. [Platform Admin Subscription Management](#platform-admin-subscription-management)
   - Get Public Pricing Plans (No Auth Required)
   - Manual Subscription Override
   - Extend Trial Period
   - Get Subscription Details
   - List Payment Transactions
   - Filter Tenants by Subscription
   - Platform Pricing Management (Database-Driven)
     - List All Subscription Plans
     - Update Subscription Plan Pricing
     - Pricing Change Audit Trail
14. [User Flows with Diagrams](#user-flows-with-diagrams)
14. [Common Patterns](#common-patterns)
15. [Frontend Pages Checklist](#frontend-pages-checklist)
16. [Next Steps](#next-steps)
17. [Important Notes](#important-notes)

---

## Authentication Overview

### Two Separate Authentication Systems

```
┌─────────────────────────────────────────────────────────┐
│                   LURAN PLATFORM                        │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────────────────┐  ┌────────────────────┐  │
│  │  Platform Admin Login    │  │  Company Login     │  │
│  │  (Luran Staff)          │  │  (Tenant Users)    │  │
│  │                          │  │                    │  │
│  │  • SUPER_ADMIN          │  │  • TENANT_ADMIN    │  │
│  │  • ADMIN                │  │  • SUPPORT_AGENT   │  │
│  │                          │  │  • VIEWER          │  │
│  │  Endpoint:               │  │  Endpoint:         │  │
│  │  /platform/auth/login    │  │  /auth/login       │  │
│  └──────────────────────────┘  └────────────────────┘  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Important:** Use different endpoints and store tokens separately!

---

## 1. Platform Admin Dashboard

Pages to build for Luran staff (platform admins).

### 1.1 Platform Admin Login

**Page:** `/platform-admin/login`

**Endpoint:** `POST /platform/auth/login`

**Request:**
```json
{
  "email": "admin@luranai.com",
  "password": "your-password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

**Frontend Action:**
- Store `access_token` in localStorage/sessionStorage as `platform_admin_token`
- Redirect to `/platform-admin/dashboard`

---

### 1.2 Get Current Platform Admin Info

**Page:** All platform admin pages (for user menu)

**Endpoint:** `GET /platform/auth/me`

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Response:**
```json
{
  "id": "uuid",
  "email": "admin@luranai.com",
  "full_name": "Admin User",
  "role": "SUPER_ADMIN",
  "is_active": true,
  "last_login_at": "2026-01-15T10:30:00Z",
  "created_at": "2025-01-01T00:00:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

---

### 1.3 Change Platform Admin Password

**Page:** `/platform-admin/settings`

**Endpoint:** `PUT /platform/auth/me/password`

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Request:**
```json
{
  "current_password": "old-password",
  "new_password": "new-password"
}
```

**Response:**
```json
{
  "message": "Password updated successfully"
}
```

---

### 1.4 Create New Platform Admin (SUPER_ADMIN only)

**Page:** `/platform-admin/admins`

**Endpoint:** `POST /platform/auth/admins`

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Request:**
```json
{
  "email": "newadmin@luranai.com",
  "full_name": "New Admin",
  "role": "ADMIN",
  "password": "secure-password"
}
```

**Response:**
```json
{
  "id": "uuid",
  "email": "newadmin@luranai.com",
  "full_name": "New Admin",
  "role": "ADMIN",
  "is_active": true,
  "last_login_at": null,
  "created_at": "2026-01-15T11:00:00Z",
  "updated_at": "2026-01-15T11:00:00Z"
}
```

---

### 1.5 List All Platform Admins (SUPER_ADMIN only)

**Page:** `/platform-admin/admins`

**Endpoint:** `GET /platform/auth/admins`

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Response:**
```json
[
  {
    "id": "uuid",
    "email": "admin@luranai.com",
    "full_name": "Admin User",
    "role": "SUPER_ADMIN",
    "is_active": true,
    "created_at": "2025-01-01T00:00:00Z"
  },
  {
    "id": "uuid",
    "email": "john@luranai.com",
    "full_name": "John Smith",
    "role": "ADMIN",
    "is_active": true,
    "created_at": "2026-01-10T00:00:00Z"
  }
]
```

---

### 1.6 List All Tenants (Platform Admin)

**Page:** `/platform-admin/tenants`

**Endpoint:** `GET /platform/tenants`

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Required Role:** SUPER_ADMIN

**Query Parameters:**
```
?status_filter=ACTIVE
?subscription_status=active
?subscription_tier=professional
?trial_expiring_days=7
?limit=100
?offset=0
```

**Parameter Details:**
- `status_filter`: Filter by tenant status - `ACTIVE`, `INACTIVE`, or `SUSPENDED`
- `subscription_status`: Filter by subscription - `trialing`, `active`, `trial_expired`, `canceled`, `past_due`
- `subscription_tier`: Filter by tier - `trial`, `lite`, `professional`, `enterprise`
- `trial_expiring_days`: Find trials expiring within N days (0-90)
- `limit`: Maximum results (1-1000, default: 100)
- `offset`: Pagination offset (default: 0)

**Response:**
```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "acme_corp",
    "display_name": "Acme Corporation",
    "description": "AI-powered customer support for Acme",
    "status": "ACTIVE",
    "subscription_tier": "professional",
    "subscription_status": "active",
    "billing_cycle": "monthly",
    "trial_ends_at": null,
    "created_at": "2026-01-10T00:00:00Z",
    "updated_at": "2026-01-15T12:00:00Z"
  },
  {
    "id": "660e9500-f39c-52e5-b827-557766551111",
    "name": "techstart_inc",
    "display_name": "TechStart Inc",
    "description": "Startup AI support",
    "status": "ACTIVE",
    "subscription_tier": "trial",
    "subscription_status": "trialing",
    "billing_cycle": null,
    "trial_ends_at": "2026-01-25T00:00:00Z",
    "created_at": "2026-01-18T00:00:00Z",
    "updated_at": "2026-01-18T00:00:00Z"
  }
]
```

**Use Cases:**
- View all customers
- Find trial_expired tenants for conversion outreach
- List enterprise customers
- Identify trials expiring in 7 days for reminder emails
- Filter past_due subscriptions for payment follow-up

**Frontend Display:**
- Table with columns: Company Name, Status, Subscription Tier, Trial Ends, Created Date
- Filters dropdown for status, subscription status, tier
- Search by company name
- Pagination controls

---

### 1.7 Get Tenant Details (Platform Admin)

**Page:** `/platform-admin/tenants/{tenant_id}`

**Endpoint:** `GET /platform/tenants/{tenant_id}`

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Required Role:** SUPER_ADMIN

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "acme_corp",
  "display_name": "Acme Corporation",
  "description": "AI-powered customer support for Acme",
  "status": "ACTIVE",
  "subscription_tier": "professional",
  "subscription_status": "active",
  "billing_cycle": "monthly",
  "trial_ends_at": null,
  "subscription_starts_at": "2026-01-10T00:00:00Z",
  "subscription_ends_at": null,
  "created_at": "2026-01-10T00:00:00Z",
  "updated_at": "2026-01-15T12:00:00Z",
  "ai_config": {
    "id": "ai-config-uuid",
    "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
    "brand_voice": {
      "tone": "professional",
      "formality_level": "balanced",
      "greeting_style": "warm",
      "emoji_usage": false
    },
    "allowed_topics": [
      "product features",
      "pricing",
      "technical support"
    ],
    "blocked_topics": [
      "competitor comparisons"
    ],
    "escalation_keywords": [
      "speak to human",
      "talk to manager"
    ],
    "confidence_threshold": 0.55,
    "max_response_tokens": 800,
    "grounding_requirement": true,
    "custom_instructions": "Always mention our 24/7 support hotline.",
    "created_at": "2026-01-10T00:00:00Z",
    "updated_at": "2026-01-15T10:00:00Z"
  },
  "capabilities": {
    "id": "capabilities-uuid",
    "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
    "can_create_tickets": true,
    "can_access_analytics": true,
    "can_upload_knowledge": true,
    "max_knowledge_mb": 200,
    "max_conversations_per_month": 5000,
    "max_knowledge_sources": 30,
    "can_use_url_sources": true,
    "max_team_members": 10,
    "created_at": "2026-01-10T00:00:00Z",
    "updated_at": "2026-01-15T11:00:00Z"
  }
}
```

**Use Case:**
- View complete tenant details including AI configuration and capabilities
- Troubleshoot customer issues
- Verify subscription settings

**Frontend Display:**
- Tenant information card (name, status, dates)
- Subscription details section
- AI configuration section (expandable)
- Capabilities/limits section
- Action buttons (Edit Capabilities, Suspend Tenant)

---

### 1.8 Deactivate Tenant (Platform Admin)

**Page:** `/platform-admin/tenants/{tenant_id}` (delete/suspend button)

**Endpoint:** `DELETE /platform/tenants/{tenant_id}`

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Required Role:** SUPER_ADMIN

**Response:**
```json
{
  "message": "Tenant deactivated successfully"
}
```

**Note:** This is a soft delete - sets tenant status to `INACTIVE`. Data is preserved for compliance and potential reactivation.

**Use Cases:**
- Subscription expired/cancelled
- Terms of service violation
- Company requested account closure
- Non-payment suspension

**Frontend Action:**
- Show confirmation dialog: "Deactivate this tenant? This will suspend their account access."
- On confirm, call API and show success message
- Update tenant list/status

**Error Response (Tenant not found):**
```json
{
  "detail": "Tenant not found"
}
```

---

### 1.9 Update Tenant Capabilities (Platform Admin)

**Page:** `/platform-admin/tenants/{tenant_id}/edit`

**Endpoint:** `PUT /platform/tenants/{tenant_id}/capabilities`

**Headers:**
```
Authorization: Bearer {platform_admin_token}
Content-Type: application/json
```

**Required Role:** SUPER_ADMIN

**Request:** (all fields optional)
```json
{
  "can_create_tickets": true,
  "can_access_analytics": true,
  "can_upload_knowledge": true,
  "max_knowledge_mb": 500,
  "max_conversations_per_month": 10000,
  "max_knowledge_sources": 50,
  "can_use_url_sources": true,
  "max_team_members": 25
}
```

**Field Descriptions:**
- `can_create_tickets`: Whether bot can create support tickets when escalating
- `can_access_analytics`: Whether tenant can view analytics dashboard
- `can_upload_knowledge`: Whether tenant can upload knowledge base documents
- `max_knowledge_mb`: Storage limit for knowledge base in MB (null = unlimited)
- `max_conversations_per_month`: Monthly conversation limit (null = unlimited)
- `max_knowledge_sources`: Total knowledge sources - files + URLs (null = unlimited)
- `can_use_url_sources`: Whether tenant can use URL sources for knowledge
- `max_team_members`: Maximum team members allowed (null = unlimited)

**Response:**
```json
{
  "id": "capabilities-uuid",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "can_create_tickets": true,
  "can_access_analytics": true,
  "can_upload_knowledge": true,
  "max_knowledge_mb": 500,
  "max_conversations_per_month": 10000,
  "max_knowledge_sources": 50,
  "can_use_url_sources": true,
  "max_team_members": 25,
  "created_at": "2026-01-10T00:00:00Z",
  "updated_at": "2026-01-20T14:30:00Z"
}
```

**Use Cases:**
- Subscription tier upgrade/downgrade
- Enable/disable features based on payment status
- Increase limits for enterprise customers
- Apply trial period restrictions
- Custom pricing/limits for special cases

**Frontend Display:**
- Form with toggles for boolean capabilities (can_create_tickets, etc.)
- Number inputs for limits (max_knowledge_mb, max_conversations_per_month, etc.)
- Checkbox for "Unlimited" that sets value to null
- Save button that calls this endpoint

**Error Response (Tenant not found):**
```json
{
  "detail": "Tenant not found"
}
```

**Frontend Action:**
- Show success message: "Capabilities updated successfully"
- Refresh tenant details
- Display updated limits

---

## 2. Company/Tenant Dashboard

Pages to build for companies using Luran (tenant users).

### 2.1 Company Registration (Public)

**Page:** `/register` (public, no auth required)

**Endpoint:** `POST /auth/register-company`

**Request:**
```json
{
  "company_name": "Acme Corp",
  "company_display_name": "Acme Corporation",
  "admin_full_name": "John Doe",
  "admin_email": "john@acmecorp.com"
}
```

**Response:**
```json
{
  "message": "Company registered successfully. Please check your email to verify your account.",
  "tenant_id": "uuid",
  "admin_email": "john@acmecorp.com",
  "verification_required": true
}
```

**Frontend Action:**
- Show success message: "Registration successful! Check your email for verification link."
- Redirect to `/verify-email-sent` page

**What Happens:**
1. System creates tenant (`acme_corp`)
2. Creates TENANT_ADMIN user (inactive until verified)
3. Generates temporary password
4. Sends email with:
   - Login credentials
   - Email verification link (expires in 24 hours)

---

### 2.2 Email Verification

**Page:** `/verify-email?token={token}` (public)

**Endpoint:** `POST /auth/verify-email`

**Request:**
```json
{
  "token": "verification-token-from-email-link"
}
```

**Response:**
```json
{
  "message": "Email verified successfully! You can now log in.",
  "verified": true
}
```

**Frontend Action:**
- Show success message
- Redirect to `/login` with message "Email verified! Please log in with your credentials."

---

### 2.3 Resend Verification Email

**Page:** `/resend-verification` (public)

**Endpoint:** `POST /auth/resend-verification?email={email}`

**Request:**
```
Query parameter: email=john@acmecorp.com
```

**Response:**
```json
{
  "message": "Verification email sent successfully"
}
```

---

### 2.4 Company Login

**Page:** `/login`

**Endpoint:** `POST /auth/login`

**Request:**
```json
{
  "email": "john@acmecorp.com",
  "password": "temp-password-from-email"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

**Frontend Action:**
- Store `access_token` in localStorage as `tenant_token`
- Redirect to `/dashboard`

---

### 2.5 Get Current User Info

**Page:** All tenant pages (for user menu)

**Endpoint:** `GET /auth/me`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "id": "uuid",
  "email": "john@acmecorp.com",
  "full_name": "John Doe",
  "role": "TENANT_ADMIN",
  "tenant_id": "uuid",
  "is_active": true,
  "is_superuser": false,
  "last_login_at": "2026-01-15T10:30:00Z",
  "created_at": "2026-01-15T10:00:00Z",
  "updated_at": "2026-01-15T10:00:00Z"
}
```

**Display:**
- User menu showing: name, email, role
- Tenant/company name

---

### 2.6 Change Password

**Page:** `/dashboard/settings/password`

**Endpoint:** `PUT /auth/me/password`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Request:**
```json
{
  "current_password": "temp-password",
  "new_password": "new-secure-password"
}
```

**Response:**
```json
{
  "message": "Password updated successfully"
}
```

---

### 2.7 Get Company Details

**Page:** `/dashboard/settings/company`

**Endpoint:** `GET /tenant`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "acme_corp",
  "display_name": "Acme Corporation",
  "description": "AI-powered customer support for Acme",
  "status": "ACTIVE",
  "created_at": "2026-01-10T00:00:00Z",
  "updated_at": "2026-01-15T12:00:00Z",
  "ai_config": {
    "id": "config-uuid",
    "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
    "brand_voice": {
      "tone": "professional",
      "formality_level": "balanced",
      "greeting_style": "warm",
      "emoji_usage": false
    },
    "allowed_topics": [
      "product features",
      "pricing",
      "technical support",
      "account management"
    ],
    "blocked_topics": [
      "competitor comparisons",
      "personal medical advice"
    ],
    "escalation_keywords": [
      "speak to human",
      "talk to manager",
      "cancel subscription",
      "refund"
    ],
    "confidence_threshold": 0.55,
    "max_response_tokens": 400,
    "grounding_requirement": true,
    "custom_instructions": "Always mention our 24/7 support hotline at the end of responses.",
    "created_at": "2026-01-10T00:00:00Z",
    "updated_at": "2026-01-15T10:00:00Z"
  },
  "capabilities": {
    "id": "capabilities-uuid",
    "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
    "can_create_tickets": true,
    "can_access_analytics": true,
    "can_upload_knowledge": true,
    "max_knowledge_mb": 100,
    "max_conversations_per_month": null,
    "created_at": "2026-01-10T00:00:00Z",
    "updated_at": "2026-01-10T00:00:00Z"
  }
}
```

**What You Get:**
- Company information (name, display name, description)
- Account status (ACTIVE, INACTIVE, or SUSPENDED)
- AI chatbot configuration (how your bot talks and behaves)
- Subscription features and limits

**Display:**
Use this data to populate your company settings dashboard showing current configuration.

---

### 2.8 Update Company Information

**Page:** `/dashboard/settings/company`

**Endpoint:** `PUT /tenant`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:** (all fields optional)
```json
{
  "display_name": "Acme Corporation Ltd.",
  "description": "Leading provider of AI-powered customer support solutions"
}
```

**What You Can Update:**
- `display_name` - Your company's friendly name (shown to customers)
- `description` - Brief description of your company

**What You Cannot Update:**
- `name` - Your unique identifier (permanent)
- `status` - Account status (only platform admins can change this)

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "acme_corp",
  "display_name": "Acme Corporation Ltd.",
  "description": "Leading provider of AI-powered customer support solutions",
  "status": "ACTIVE",
  "created_at": "2026-01-10T00:00:00Z",
  "updated_at": "2026-01-16T14:30:00Z",
  "ai_config": { ... },
  "capabilities": { ... }
}
```

---

### 2.9 Get AI Configuration

**Page:** `/dashboard/settings/ai`

**Endpoint:** `GET /tenant/ai-config`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "id": "config-uuid",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "brand_voice": {
    "tone": "professional",
    "formality_level": "balanced",
    "greeting_style": "warm",
    "emoji_usage": false
  },
  "allowed_topics": [
    "product features",
    "pricing",
    "technical support"
  ],
  "blocked_topics": [
    "competitor comparisons",
    "personal medical advice"
  ],
  "escalation_keywords": [
    "speak to human",
    "talk to manager",
    "cancel subscription"
  ],
  "confidence_threshold": 0.55,
  "max_response_tokens": 400,
  "grounding_requirement": true,
  "custom_instructions": "Always mention our 24/7 support hotline.",
  "created_at": "2026-01-10T00:00:00Z",
  "updated_at": "2026-01-15T10:00:00Z"
}
```

**Field Explanations:**

- **brand_voice** - How your chatbot talks:
  - `tone`: "professional", "friendly", or "casual"
  - `formality_level`: "formal", "balanced", or "casual"
  - `greeting_style`: "warm", "neutral", or "enthusiastic"
  - `emoji_usage`: true/false - whether to use emojis

- **allowed_topics** - Topics your bot can discuss (null = all topics allowed)

- **blocked_topics** - Topics your bot should avoid

- **escalation_keywords** - Words that trigger transfer to human support

- **confidence_threshold** - Minimum confidence score (0.0 to 1.0) before responding. Lower = more responses, higher = transfers more to humans

- **max_response_tokens** - Maximum length of bot responses (50 to 2000)

- **grounding_requirement** - true = bot must use knowledge base, false = can answer without it

- **custom_instructions** - Additional rules for your bot

---

### 2.10 Update AI Configuration

**Page:** `/dashboard/settings/ai`

**Endpoint:** `PUT /tenant/ai-config`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:** (all fields optional)
```json
{
  "brand_voice": {
    "tone": "friendly",
    "formality_level": "casual",
    "greeting_style": "enthusiastic",
    "emoji_usage": true
  },
  "allowed_topics": [
    "product features",
    "pricing",
    "billing",
    "technical support",
    "account management"
  ],
  "blocked_topics": [
    "competitor comparisons",
    "political discussions"
  ],
  "escalation_keywords": [
    "speak to human",
    "talk to manager",
    "supervisor",
    "cancel",
    "refund",
    "complaint"
  ],
  "confidence_threshold": 0.60,
  "max_response_tokens": 350,
  "grounding_requirement": true,
  "custom_instructions": "Always be empathetic. Mention our 24/7 hotline: 1-800-ACME-HELP"
}
```

**Tips for Configuration:**

1. **Brand Voice:**
   - Match your company's communication style
   - Consistent tone builds trust

2. **Topics:**
   - Leave `allowed_topics` as null to allow all topics
   - Use `blocked_topics` to prevent sensitive discussions

3. **Escalation Keywords:**
   - Include words that indicate customer frustration
   - Add product-specific terms (e.g., "cancel subscription")

4. **Confidence Threshold:**
   - 0.50-0.60: More automated responses
   - 0.60-0.70: Balanced (recommended)
   - 0.70+: More human transfers (higher quality)

5. **Response Length:**
   - 200-300: Concise answers
   - 300-500: Detailed explanations (recommended)
   - 500+: Very detailed (may be too long)

**Response:**
```json
{
  "id": "config-uuid",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "brand_voice": {
    "tone": "friendly",
    "formality_level": "casual",
    "greeting_style": "enthusiastic",
    "emoji_usage": true
  },
  "allowed_topics": [
    "product features",
    "pricing",
    "billing",
    "technical support",
    "account management"
  ],
  "blocked_topics": [
    "competitor comparisons",
    "political discussions"
  ],
  "escalation_keywords": [
    "speak to human",
    "talk to manager",
    "supervisor",
    "cancel",
    "refund",
    "complaint"
  ],
  "confidence_threshold": 0.60,
  "max_response_tokens": 350,
  "grounding_requirement": true,
  "custom_instructions": "Always be empathetic. Mention our 24/7 hotline: 1-800-ACME-HELP",
  "created_at": "2026-01-10T00:00:00Z",
  "updated_at": "2026-01-16T15:00:00Z"
}
```

**Frontend Action:**
- Show success message: "AI configuration updated successfully"
- Changes take effect immediately on next customer chat

---

### 2.11 View Subscription Features

**Page:** `/dashboard/settings/subscription`

**Endpoint:** `GET /tenant/capabilities`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "id": "capabilities-uuid",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "can_create_tickets": true,
  "can_access_analytics": true,
  "can_upload_knowledge": true,
  "max_knowledge_mb": 100,
  "max_conversations_per_month": null,
  "created_at": "2026-01-10T00:00:00Z",
  "updated_at": "2026-01-10T00:00:00Z"
}
```

**Field Explanations:**

- **can_create_tickets** - Whether bot can create support tickets when escalating to humans

- **can_access_analytics** - Whether you can view conversation analytics dashboard

- **can_upload_knowledge** - Whether you can upload knowledge base documents

- **max_knowledge_mb** - Storage limit for knowledge base in megabytes

- **max_conversations_per_month** - Monthly conversation limit (null = unlimited)

**Display:**
Show this as a read-only view of your current subscription tier.

**To Upgrade:**
Display "Contact support to upgrade" button that opens:
- Email: support@luranai.com
- Subject: "Upgrade Request - [Your Company Name]"
- Or link to pricing page

**Note:** Only platform admins can modify capabilities. You can view but not change these settings.

---

## 3. Team Management

### 3.1 Invite Team Member

**Page:** `/dashboard/team`

**Endpoint:** `POST /users`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```json
{
  "email": "sarah@acmecorp.com",
  "full_name": "Sarah Johnson",
  "role": "SUPPORT_AGENT"
}
```

**Allowed Roles:**
- `SUPPORT_AGENT` - Can handle customer support
- `VIEWER` - Read-only access

**Response:**
```json
{
  "id": "uuid",
  "email": "sarah@acmecorp.com",
  "full_name": "Sarah Johnson",
  "role": "SUPPORT_AGENT",
  "tenant_id": "uuid",
  "is_active": true,
  "is_superuser": false,
  "last_login_at": null,
  "created_at": "2026-01-15T11:00:00Z",
  "updated_at": "2026-01-15T11:00:00Z"
}
```

**Frontend Action:**
- Show success message: "Invitation sent to sarah@acmecorp.com"
- Refresh team member list

**What Happens:**
1. System creates user
2. Generates temporary password
3. Sends invite email with credentials

---

### 3.2 List Team Members

**Page:** `/dashboard/team`

**Endpoint:** `GET /users`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
[
  {
    "id": "uuid",
    "email": "john@acmecorp.com",
    "full_name": "John Doe",
    "role": "TENANT_ADMIN",
    "tenant_id": "uuid",
    "is_active": true,
    "created_at": "2026-01-15T10:00:00Z"
  },
  {
    "id": "uuid",
    "email": "sarah@acmecorp.com",
    "full_name": "Sarah Johnson",
    "role": "SUPPORT_AGENT",
    "tenant_id": "uuid",
    "is_active": true,
    "created_at": "2026-01-15T11:00:00Z"
  }
]
```

---

### 3.3 Get Team Member Details

**Page:** `/dashboard/team/{user_id}`

**Endpoint:** `GET /users/{user_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "id": "uuid",
  "email": "sarah@acmecorp.com",
  "full_name": "Sarah Johnson",
  "role": "SUPPORT_AGENT",
  "tenant_id": "uuid",
  "is_active": true,
  "is_superuser": false,
  "last_login_at": "2026-01-15T11:30:00Z",
  "created_at": "2026-01-15T11:00:00Z",
  "updated_at": "2026-01-15T11:00:00Z"
}
```

---

### 3.4 Update Team Member

**Page:** `/dashboard/team/{user_id}/edit`

**Endpoint:** `PATCH /users/{user_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Request:** (all fields optional)
```json
{
  "full_name": "Sarah J. Johnson",
  "role": "VIEWER",
  "is_active": true
}
```

**Response:**
```json
{
  "id": "uuid",
  "email": "sarah@acmecorp.com",
  "full_name": "Sarah J. Johnson",
  "role": "VIEWER",
  "tenant_id": "uuid",
  "is_active": true,
  "is_superuser": false,
  "last_login_at": "2026-01-15T11:30:00Z",
  "created_at": "2026-01-15T11:00:00Z",
  "updated_at": "2026-01-15T12:00:00Z"
}
```

---

### 3.5 Deactivate Team Member

**Page:** `/dashboard/team` (delete button)

**Endpoint:** `DELETE /users/{user_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "message": "User deactivated successfully"
}
```

**Note:** Cannot deactivate TENANT_ADMIN users

---

### 3.6 Regenerate Member Password

**Page:** `/dashboard/team/{user_id}` (reset password button)

**Endpoint:** `POST /users/{user_id}/regenerate-password`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "message": "Password regenerated successfully",
  "temp_password": "new-temp-password-12#A",
  "email_sent": true
}
```

**Frontend Action:**
- Show temp password to admin (one-time)
- Show message: "New password sent to user's email"

---

## 4. API Key Management

### 4.1 Create API Key

**Page:** `/dashboard/settings/api-keys`

**Endpoint:** `POST /api-keys`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```json
{
  "name": "Production Widget Key",
  "key_type": "PUBLIC",
  "expires_at": null
}
```

**Key Types:**
- `PUBLIC` - For widget (starts with `pk_live_...`)
- `SECRET` - For server-to-server (starts with `sk_live_...`)

**Response:**
```json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "key_type": "PUBLIC",
  "key_prefix": "pk_live_abc123...",
  "name": "Production Widget Key",
  "is_active": true,
  "last_used_at": null,
  "expires_at": null,
  "created_at": "2026-01-15T12:00:00Z",
  "updated_at": "2026-01-15T12:00:00Z",
  "full_key": "pk_live_abc123def456ghi789jkl012mno345pqr678stu901vwx"
}
```

**IMPORTANT:**
- `full_key` is only shown ONCE during creation
- Display a modal: "Save this key! It won't be shown again."
- Provide copy-to-clipboard button

---

### 4.2 List API Keys

**Page:** `/dashboard/settings/api-keys`

**Endpoint:** `GET /api-keys`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
[
  {
    "id": "uuid",
    "name": "Production Widget Key",
    "key_type": "PUBLIC",
    "key_prefix": "pk_live_abc123...",
    "is_active": true,
    "last_used_at": "2026-01-15T11:30:00Z",
    "expires_at": null,
    "created_at": "2026-01-15T10:00:00Z"
  },
  {
    "id": "uuid",
    "name": "Development Key",
    "key_type": "PUBLIC",
    "key_prefix": "pk_live_xyz789...",
    "is_active": false,
    "last_used_at": null,
    "expires_at": null,
    "created_at": "2026-01-14T09:00:00Z"
  }
]
```

**Display:**
- Table with columns: Name, Type, Key Prefix, Status, Last Used, Created
- Actions: Edit, Deactivate/Activate, Delete

---

### 4.3 Update API Key

**Page:** `/dashboard/settings/api-keys/{key_id}/edit`

**Endpoint:** `PATCH /api-keys/{key_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Request:** (all fields optional)
```json
{
  "name": "Production Widget Key v2",
  "is_active": false
}
```

**Response:**
```json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "key_type": "PUBLIC",
  "key_prefix": "pk_live_abc123...",
  "name": "Production Widget Key v2",
  "is_active": false,
  "last_used_at": "2026-01-15T11:30:00Z",
  "expires_at": null,
  "created_at": "2026-01-15T10:00:00Z",
  "updated_at": "2026-01-15T12:30:00Z"
}
```

---

### 4.4 Delete API Key

**Page:** `/dashboard/settings/api-keys` (delete button)

**Endpoint:** `DELETE /api-keys/{key_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "message": "API key deleted successfully"
}
```

**Frontend Action:**
- Show confirmation dialog: "Are you sure? This will break any widgets using this key."

---

## 5. Widget Configuration

### 5.1 Get Widget Configuration

**Page:** `/dashboard/widget/setup`

**Endpoint:** `GET /widget/config`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "tenant_id": "uuid",
  "company_name": "Acme Corporation",
  "widget_primary_color": "#2563eb",
  "widget_accent_color": "#10b981",
  "widget_logo_url": "https://acmecorp.com/logo.png",
  "widget_greeting_message": "Hi! How can we help you today?",
  "widget_position": "bottom-right",
  "widget_show_unread_badge": true,
  "widget_allow_file_upload": true
}
```

---

### 5.2 Update Widget Configuration (Colors, Text, URL)

**Page:** `/dashboard/widget/setup`

**Endpoint:** `PATCH /widget/config`

**Headers:**
```
Authorization: Bearer {tenant_token}
Content-Type: application/json
```

**IMPORTANT:** This endpoint accepts JSON data. Use this to update:
- Colors (primary/accent)
- Greeting message
- Widget position
- Logo URL (if you already have a hosted image URL)

For uploading logo **image files**, use section 5.3 below instead.

**Request:** (all fields optional)
```json
{
  "widget_primary_color": "#ff5733",
  "widget_accent_color": "#33ff57",
  "widget_logo_url": "https://acmecorp.com/new-logo.png",
  "widget_greeting_message": "Welcome to Acme! How can we assist you?",
  "widget_position": "bottom-left",
  "widget_show_unread_badge": false,
  "widget_allow_file_upload": true
}
```

**Validation:**
- Colors must be hex format: `#rrggbb`
- Position must be: `bottom-right`, `bottom-left`, `top-right`, or `top-left`
- Greeting message max 500 characters
- Logo URL max 500 characters

**Response:**
```json
{
  "tenant_id": "uuid",
  "company_name": "Acme Corporation",
  "widget_primary_color": "#ff5733",
  "widget_accent_color": "#33ff57",
  "widget_logo_url": "https://acmecorp.com/new-logo.png",
  "widget_greeting_message": "Welcome to Acme! How can we assist you?",
  "widget_position": "bottom-left",
  "widget_show_unread_badge": false,
  "widget_allow_file_upload": true
}
```

**Frontend:**
- Live preview of widget with updated colors/logo
- Color picker for primary/accent colors
- Text input for logo URL (if user has existing hosted image)
- Textarea for greeting message
- Dropdown for position

---

### 5.3 Upload Widget Logo (Image File)

**Page:** `/dashboard/widget/setup`

**Endpoint:** `POST /widget/upload-logo`

**Headers:**
```
Authorization: Bearer {tenant_token}
Content-Type: multipart/form-data
```

**IMPORTANT:** This endpoint accepts **multipart/form-data** (file upload). Do NOT send JSON.

**Request (Form Data):**
```
file: [File object - JPEG, PNG, GIF, or WebP]
```

**Supported Image Formats:**
- JPEG (.jpg, .jpeg)
- PNG (.png)
- GIF (.gif)
- WebP (.webp)

**File Constraints:**
- Max file size: 5MB
- Max dimensions: 4000×4000 pixels
- Recommended: 200×200 to 500×500 (square logos work best)

**Automatic Optimizations:**
- Resized to max 500×500 (maintains aspect ratio)
- Quality optimized for web
- Format converted to best option (WebP for modern browsers)
- Stored on Cloudinary CDN for fast global delivery

**Response:**
```json
{
  "message": "Logo uploaded successfully",
  "logo_url": "https://res.cloudinary.com/your_cloud/image/upload/v1234567890/luran/widget_logos/tenant-uuid_logo.png"
}
```

**Frontend Implementation Example:**

```javascript
// HTML
<input type="file" id="logoUpload" accept="image/jpeg,image/png,image/gif,image/webp">

// JavaScript
const fileInput = document.getElementById('logoUpload');
const file = fileInput.files[0];

const formData = new FormData();
formData.append('file', file);

fetch('/api/v1/widget/upload-logo', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${tenantToken}`
    // DO NOT set Content-Type header - browser sets it automatically with boundary
  },
  body: formData
})
.then(res => res.json())
.then(data => {
  console.log('Logo URL:', data.logo_url);
  // Update preview with new logo
});
```

**Error Responses:**

**File too large (>5MB):**
```json
{
  "detail": "File size (7.32MB) exceeds maximum allowed (5MB)"
}
```

**Invalid format:**
```json
{
  "detail": "Image format 'BMP' not allowed. Allowed: JPEG, PNG, GIF, WebP"
}
```

**Cloudinary not configured:**
```json
{
  "detail": "Cloudinary is not configured. Please add credentials to .env"
}
```

**Frontend Action:**
- Show file upload button with preview
- Display current logo if exists
- On upload success:
  - Update preview with new logo URL
  - Show success message: "Logo uploaded successfully"
  - Logo URL is automatically saved to widget configuration

**What Happens:**
1. Image uploaded to backend
2. Validated (format, size, dimensions)
3. Uploaded to Cloudinary with optimizations
4. URL automatically saved to `widget_logo_url` in database
5. Cloudinary URL returned to frontend
6. Logo served via CDN (fast global delivery)

---

### 5.4 Delete Widget Logo

**Page:** `/dashboard/widget/setup`

**Endpoint:** `DELETE /widget/logo`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**IMPORTANT:** No request body needed for DELETE requests.

**Response:**
```json
{
  "message": "Logo deleted successfully"
}
```

**Error Response (No logo exists):**
```json
{
  "detail": "No logo configured"
}
```

**Frontend Action:**
- Show delete/remove button next to logo preview
- On delete success:
  - Remove logo preview
  - Reset to default placeholder
  - Show message: "Logo removed"

**What Happens:**
1. Logo deleted from Cloudinary
2. `widget_logo_url` set to null in database
3. Widget will display without logo

---

### 5.5 Get Installation Code

**Page:** `/dashboard/widget/install`

**Endpoint:** `GET /widget/install-code`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "html_snippet": "<!-- Luran Widget Integration -->\n<script>\n  window.luranSettings = {\n    tenantId: 'a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6',\n    apiKey: 'YOUR_PUBLIC_API_KEY_HERE'  // Get from API Keys page\n  };\n</script>\n<script src=\"https://cdn.luranai.com/widget/v1/widget.js\" async defer></script>\n<!-- End Luran Widget -->",
  "instructions": "Installation Instructions:\n\n1. Get your Public API Key:\n   - Go to Settings → API Keys\n   - Create a new PUBLIC API key\n   - Copy the key (starts with pk_live_...)\n\n2. Replace 'YOUR_PUBLIC_API_KEY_HERE' in the code above with your actual public API key\n\n3. Add the code snippet to your website:\n   - Paste it before the closing </body> tag\n   - The widget will appear on all pages where this code is present\n\n4. Customize the widget:\n   - Go to Settings → Widget Configuration\n   - Adjust colors, logo, greeting message, and position\n\n5. Test the widget:\n   - Visit your website\n   - Click the floating chat button\n   - Start a conversation\n\nNote: The tenantId is your Tenant UUID (pre-filled), not your company name. The widget loads asynchronously and won't affect your page load speed."
}
```

**Note:** The actual API response includes your tenant's UUID in the `tenantId` field (not company name).

**Frontend:**
- Display code in syntax-highlighted code block
- Copy-to-clipboard button
- Step-by-step installation instructions
- "Test Widget" button that opens preview

---

## 6. Widget Public API (No Auth Required)

### 6.1 Get Widget Configuration (Public)

**Used by:** Embedded widget JavaScript

**Endpoint:** `GET /widget/public/config/{tenant_id}`

**Important:** The `tenant_id` parameter must be your **Tenant UUID** (not company name).

**Headers:**
```
X-API-Key: pk_live_abc123...
```

**Example:**
```
GET /widget/public/config/a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6
X-API-Key: pk_live_abc123def456ghi789
```

**Response:**
```json
{
  "tenant_id": "uuid-string",
  "company_name": "Acme Corporation",
  "primary_color": "#2563eb",
  "accent_color": "#10b981",
  "logo_url": "https://acmecorp.com/logo.png",
  "greeting_message": "Hi! How can we help you today?",
  "position": "bottom-right",
  "show_unread_badge": true,
  "allow_file_upload": true
}
```

**Use Case:**
- Widget JavaScript calls this on page load
- Gets tenant branding configuration
- Applies styles and shows greeting

---

## 7. Knowledge Management

### What Type of Content to Upload?

Luran's AI system works with **ANY form of text-based knowledge**. You can upload:

**✅ Supported Formats:**

1. **Long-Form Documents** (Recommended)
   - Help documentation
   - User guides
   - Product manuals
   - Standard Operating Procedures (SOPs)
   - Policy documents
   - Training materials
   - Technical specifications

2. **Q&A Format** (Also Supported)
   - FAQ lists
   - Question-Answer pairs
   - Troubleshooting guides
   - "How-to" lists

3. **Mixed Content**
   - Blog posts with headings
   - Wiki-style articles
   - Knowledge base articles
   - Structured text with sections

**📄 Supported File Types:**
- `.txt` - Plain text files
- `.md` - Markdown files
- `.pdf` - PDF documents
- `.docx` - Microsoft Word documents

**💡 Best Practices:**

1. **Be Comprehensive**: Longer, detailed documents work better than short snippets
2. **Use Clear Structure**: Headings, paragraphs, bullet points
3. **Include Context**: Don't just provide answers, explain why and how
4. **Natural Language**: Write as you would explain to a human
5. **Avoid Jargon**: Unless explaining technical terms
6. **Update Regularly**: Keep knowledge fresh and accurate

**Example - Good Long-Form Document:**
```
# How to Reset Your Password

If you've forgotten your password, follow these steps:

## Web Application

1. Go to the login page
2. Click "Forgot Password" below the login button
3. Enter your registered email address
4. Check your email for a reset link (expires in 24 hours)
5. Click the link and create a new password
6. Password must be at least 8 characters with uppercase, lowercase, and numbers

## Mobile App

1. Open the app and tap "Login"
2. Tap "Forgot Password?"
3. Enter your email and tap "Send Reset Link"
4. Check your email and follow the instructions

## Troubleshooting

If you don't receive the email:
- Check your spam/junk folder
- Verify the email address is correct
- Wait 5 minutes and try again
- Contact support if still no email

For security reasons, you can only request 3 password resets per hour.
```

**Example - Good Q&A Format:**
```
Q: How do I reset my password?
A: Click "Forgot Password" on the login page, enter your email, and follow the link sent to your inbox. The link expires in 24 hours.

Q: What if I don't receive the reset email?
A: Check your spam folder, verify your email address is correct, and wait 5 minutes before trying again. You can request up to 3 resets per hour.

Q: What are the password requirements?
A: Passwords must be at least 8 characters long and include uppercase letters, lowercase letters, and numbers.
```

**❌ What NOT to Upload:**
- Raw code without explanation
- Unstructured data dumps
- Binary files or images (text will be extracted, but quality may vary)
- Extremely short snippets (less than 50 words)
- Duplicate content
- Sensitive customer data or PII

---

### 7.1 Upload Knowledge (Text Content)

**Page:** `/dashboard/knowledge/upload`

**Endpoint:** `POST /knowledge/upload`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```json
{
  "tenant_id": "uuid",
  "document_name": "Password Reset Guide",
  "document_text": "# How to Reset Your Password\n\nIf you've forgotten your password...",
  "source_type": "help_doc",
  "metadata": {
    "author": "John Doe",
    "category": "Account Management",
    "language": "en",
    "version": "2.1"
  }
}
```

**Source Types:**
- `faq` - Frequently Asked Questions
- `help_doc` - Help documentation
- `policy` - Company policies
- `sop` - Standard Operating Procedures
- `other` - Other knowledge types

**Response:**
```json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "name": "Password Reset Guide",
  "source_type": "help_doc",
  "meta": {
    "author": "John Doe",
    "category": "Account Management"
  },
  "file_url": null,
  "content_hash": "abc123def456...",
  "version": 1,
  "status": "active",
  "chunk_count": 12,
  "uploaded_by": "uuid",
  "created_at": "2026-01-15T12:00:00Z",
  "updated_at": "2026-01-15T12:00:00Z"
}
```

**Frontend Action:**
- Show success message with chunk count
- Redirect to knowledge list
- Display: "Document uploaded! 12 chunks created and indexed."

**What Happens:**
1. System validates the text (minimum 10 characters)
2. Splits text into overlapping chunks (~400 tokens each)
3. Generates embeddings for each chunk using Google AI
4. Stores chunks in PostgreSQL
5. Stores vector embeddings in Qdrant
6. Makes knowledge immediately searchable by AI

---

### 7.2 Upload Knowledge (File Upload)

**Page:** `/dashboard/knowledge/upload`

**Endpoint:** `POST /knowledge/upload-file`

**Headers:**
```
Authorization: Bearer {tenant_token}
Content-Type: multipart/form-data
```

**Required Role:** TENANT_ADMIN

**Request (Form Data):**
```
file: [File object - .txt, .md, .pdf, .docx]
tenant_id: uuid
source_type: help_doc
```

**File Limits:**
- Maximum size: 10 MB
- Supported formats: .txt, .md, .pdf, .docx

**Response:**
```json
{
  "job_id": "uuid",
  "status": "pending",
  "message": "Document upload queued for processing. Check status via provided URL or connect to WebSocket.",
  "check_status_url": "/api/v1/jobs/uuid",
  "estimated_chunks": 45,
  "estimated_time_seconds": 270
}
```

**Frontend Action:**
1. Show upload progress bar
2. Display: "Processing... This may take a few minutes"
3. Poll job status or connect to WebSocket for real-time updates
4. Show completion: "Document processed! 45 chunks created."

**What Happens:**
1. File uploaded and validated
2. Text extracted based on file type:
   - .txt/.md: Direct UTF-8 read
   - .pdf: PyPDF2 extraction
   - .docx: python-docx extraction
3. Job queued in background (async processing)
4. Chunks created and embedded (rate-limited: 10 requests/minute to Google AI)
5. Progress updates via WebSocket or polling

---

### 7.3 List Knowledge Sources

**Page:** `/dashboard/knowledge`

**Endpoint:** `GET /knowledge/tenants/{tenant_id}/sources`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Query Parameters:**
```
?status=active
```

**Status Values:**
- `processing` - Being chunked/embedded
- `active` - Ready and searchable
- `inactive` - Disabled
- `failed` - Processing failed

**Response:**
```json
[
  {
    "id": "uuid",
    "tenant_id": "uuid",
    "name": "Password Reset Guide.pdf",
    "source_type": "help_doc",
    "meta": {
      "original_filename": "password_reset.pdf",
      "file_size": 245632,
      "category": "Account Management"
    },
    "file_url": null,
    "content_hash": "abc123...",
    "version": 1,
    "status": "active",
    "chunk_count": 12,
    "uploaded_by": "uuid",
    "created_at": "2026-01-15T12:00:00Z",
    "updated_at": "2026-01-15T12:00:00Z"
  },
  {
    "id": "uuid",
    "tenant_id": "uuid",
    "name": "Billing FAQ",
    "source_type": "faq",
    "meta": {
      "author": "Support Team"
    },
    "file_url": null,
    "content_hash": "def456...",
    "version": 1,
    "status": "active",
    "chunk_count": 8,
    "uploaded_by": "uuid",
    "created_at": "2026-01-14T10:00:00Z",
    "updated_at": "2026-01-14T10:00:00Z"
  }
]
```

**Display:**
- Table with columns: Name, Type, Chunks, Status, Uploaded, Actions
- Filter by status dropdown
- Search by name
- Badges for source_type (FAQ, Help Doc, Policy, etc.)
- Status indicators (Active, Processing, Failed)

---

### 7.4 Get Knowledge Source Details

**Page:** `/dashboard/knowledge/{source_id}`

**Endpoint:** `GET /knowledge/sources/{source_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "name": "Password Reset Guide",
  "source_type": "help_doc",
  "meta": {
    "author": "John Doe",
    "category": "Account Management"
  },
  "file_url": null,
  "content_hash": "abc123...",
  "version": 1,
  "status": "active",
  "chunk_count": 12,
  "uploaded_by": "uuid",
  "created_at": "2026-01-15T12:00:00Z",
  "updated_at": "2026-01-15T12:00:00Z",
  "chunks": [
    {
      "id": "uuid",
      "tenant_id": "uuid",
      "source_id": "uuid",
      "content": "# How to Reset Your Password\n\nIf you've forgotten your password, follow these steps...",
      "chunk_index": 0,
      "vector_id": "qdrant_vector_id_1",
      "meta": {
        "section": "Introduction",
        "page": 1
      },
      "visibility": "public",
      "language": "en",
      "version": 1,
      "is_active": true,
      "created_at": "2026-01-15T12:00:00Z",
      "updated_at": "2026-01-15T12:00:00Z"
    }
  ]
}
```

**Display:**
- Document metadata (name, type, status, chunks, date)
- List of all chunks with preview
- Search within chunks
- Download/export option

---

### 7.5 Get Source Chunks

**Page:** `/dashboard/knowledge/{source_id}/chunks`

**Endpoint:** `GET /knowledge/sources/{source_id}/chunks`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Query Parameters:**
```
?active_only=true
```

**Response:**
```json
[
  {
    "id": "uuid",
    "tenant_id": "uuid",
    "source_id": "uuid",
    "content": "Chunk text here...",
    "chunk_index": 0,
    "vector_id": "qdrant_id",
    "meta": {},
    "visibility": "public",
    "language": "en",
    "version": 1,
    "is_active": true,
    "created_at": "2026-01-15T12:00:00Z",
    "updated_at": "2026-01-15T12:00:00Z"
  }
]
```

---

### 7.6 Delete Knowledge Source

**Page:** `/dashboard/knowledge` (delete button)

**Endpoint:** `DELETE /knowledge/sources/{source_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "message": "Knowledge source deleted successfully"
}
```

**Frontend Action:**
- Show confirmation dialog: "Delete this knowledge source? The AI will no longer use this information."
- On confirm, delete and refresh list
- Show success: "Knowledge source deleted"

**What Happens:**
- Source status set to inactive
- All chunks marked as inactive (soft delete)
- Vectors remain in Qdrant but won't be retrieved
- Can be restored by admin if needed

---

### 7.7 Check Upload Job Status

**Page:** `/dashboard/knowledge/processing`

**Endpoint:** `GET /jobs/{job_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "job_type": "document_ingestion",
  "status": "processing",
  "progress_percentage": 65,
  "total_items": 45,
  "processed_items": 29,
  "failed_items": 0,
  "result_data": null,
  "error_message": null,
  "created_at": "2026-01-15T12:00:00Z",
  "updated_at": "2026-01-15T12:03:00Z",
  "completed_at": null
}
```

**Status Values:**
- `pending` - Queued, waiting to start
- `processing` - Currently being processed
- `completed` - Successfully finished
- `failed` - Processing failed
- `cancelled` - Manually cancelled

**Frontend:**
- Progress bar showing `progress_percentage`
- "Processing 29 of 45 chunks..."
- Auto-refresh every 2 seconds or use WebSocket

**WebSocket Alternative:**
```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/jobs/{job_id}');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  // update.progress_percentage
  // update.status
  // update.processed_items
};
```

---

## 8. Agent Support Dashboard

For support agents and tenant admins handling escalated customer conversations.

### 8.1 List Escalated Conversations

**Page:** `/dashboard/support/conversations`

**Endpoint:** `GET /chat/tenants/{tenant_id}/sessions`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Query Parameters:**
```
?status=escalated
?limit=100
```

**Status Values:**
- `active` - Normal AI conversation
- `escalated` - Transferred to human support
- `resolved` - Issue resolved
- `abandoned` - Customer stopped responding

**Example Request:**
```
GET /chat/tenants/550e8400-e29b-41d4-a716-446655440000/sessions?status=escalated&limit=100
```

**Response:**
```json
[
  {
    "id": "session-uuid-1",
    "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
    "user_identifier": "customer@example.com",
    "status": "escalated",
    "escalation_reason": "Customer requested to speak with human agent",
    "escalated_at": "2026-01-18T10:30:00Z",
    "agent_id": null,
    "agent_joined_at": null,
    "confidence_scores": [0.45, 0.42, 0.38],
    "meta": {
      "user_agent": "Mozilla/5.0...",
      "ip_hash": "abc123..."
    },
    "last_message_at": "2026-01-18T10:35:00Z",
    "created_at": "2026-01-18T10:15:00Z",
    "updated_at": "2026-01-18T10:30:00Z"
  },
  {
    "id": "session-uuid-2",
    "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
    "user_identifier": "user@domain.com",
    "status": "escalated",
    "escalation_reason": "Low AI confidence score (0.38)",
    "escalated_at": "2026-01-18T09:45:00Z",
    "agent_id": "agent-uuid-1",
    "agent_joined_at": "2026-01-18T09:50:00Z",
    "confidence_scores": [0.55, 0.40, 0.38],
    "meta": {},
    "last_message_at": "2026-01-18T10:20:00Z",
    "created_at": "2026-01-18T09:30:00Z",
    "updated_at": "2026-01-18T09:50:00Z"
  }
]
```

**Display:**
- Table showing: Customer ID, Status, Escalation Reason, Escalated At, Agent, Last Message, Actions
- Filter by status dropdown (escalated, resolved, abandoned)
- Sort by escalation time (most recent first)
- "Take Over" button for unassigned sessions
- "View Details" button to open conversation

**What Happens:**
- Retrieves all sessions with specified status for the tenant
- Filtered by tenant automatically based on authenticated user
- Ordered by escalation time (most recent first)
- Shows which sessions have assigned agents

---

### 8.2 View Conversation Details

**Page:** `/dashboard/support/conversations/{session_id}`

**Endpoint:** `GET /chat/sessions/{session_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Response:**
```json
{
  "id": "session-uuid-1",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_identifier": "customer@example.com",
  "status": "escalated",
  "escalation_reason": "Customer requested to speak with human agent",
  "escalated_at": "2026-01-18T10:30:00Z",
  "agent_id": "agent-uuid-1",
  "agent_joined_at": "2026-01-18T10:35:00Z",
  "confidence_scores": [0.45, 0.42, 0.38],
  "meta": {
    "user_agent": "Mozilla/5.0...",
    "ip_hash": "abc123..."
  },
  "last_message_at": "2026-01-18T10:40:00Z",
  "created_at": "2026-01-18T10:15:00Z",
  "updated_at": "2026-01-18T10:35:00Z",
  "messages": [
    {
      "id": "message-uuid-1",
      "session_id": "session-uuid-1",
      "role": "user",
      "content": "I need help with my billing issue",
      "retrieved_chunk_ids": null,
      "confidence_score": null,
      "token_count": 8,
      "meta": {},
      "created_at": "2026-01-18T10:15:00Z"
    },
    {
      "id": "message-uuid-2",
      "session_id": "session-uuid-1",
      "role": "assistant",
      "content": "I can help you with billing questions. What specific issue are you experiencing?",
      "retrieved_chunk_ids": ["chunk-uuid-1", "chunk-uuid-2"],
      "confidence_score": 0.45,
      "token_count": 15,
      "meta": {
        "model_version": "gemini-1.5-pro"
      },
      "created_at": "2026-01-18T10:15:30Z"
    },
    {
      "id": "message-uuid-3",
      "session_id": "session-uuid-1",
      "role": "user",
      "content": "I want to speak to a human",
      "retrieved_chunk_ids": null,
      "confidence_score": null,
      "token_count": 7,
      "meta": {},
      "created_at": "2026-01-18T10:16:00Z"
    },
    {
      "id": "message-uuid-4",
      "session_id": "session-uuid-1",
      "role": "system",
      "content": "Agent Sarah Johnson joined the conversation",
      "retrieved_chunk_ids": null,
      "confidence_score": null,
      "token_count": null,
      "meta": {
        "event": "agent_joined",
        "agent_id": "agent-uuid-1"
      },
      "created_at": "2026-01-18T10:35:00Z"
    },
    {
      "id": "message-uuid-5",
      "session_id": "session-uuid-1",
      "role": "human_agent",
      "content": "Hi! I'm Sarah, I'm here to help with your billing issue. Can you tell me more about what's happening?",
      "retrieved_chunk_ids": null,
      "confidence_score": null,
      "token_count": null,
      "meta": {
        "agent_id": "agent-uuid-1",
        "agent_name": "Sarah Johnson"
      },
      "created_at": "2026-01-18T10:35:30Z"
    }
  ]
}
```

**Message Role Values:**
- `user` - Customer message
- `assistant` - AI response
- `human_agent` - Agent response
- `system` - System notification

**Display:**
- Chat interface showing full conversation history
- Different styling for each message role:
  - User messages: Right-aligned, blue
  - AI messages: Left-aligned, gray, with confidence score badge
  - Agent messages: Left-aligned, green, with agent name
  - System messages: Center-aligned, italic
- Session metadata sidebar: Customer ID, Status, Escalation reason, Timestamps
- "Take Over" button if not assigned
- Message input box if agent has taken over

**What Happens:**
- Retrieves session with complete message history
- Tenant access verified automatically
- Messages ordered by creation time (oldest first)
- Includes all metadata for each message

---

### 8.3 Get Message History

**Page:** `/dashboard/support/conversations/{session_id}` (alternative loading method)

**Endpoint:** `GET /chat/sessions/{session_id}/messages`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Query Parameters:**
```
?limit=100
```

**Response:**
```json
[
  {
    "id": "message-uuid-1",
    "session_id": "session-uuid-1",
    "role": "user",
    "content": "I need help with my billing issue",
    "retrieved_chunk_ids": null,
    "confidence_score": null,
    "token_count": 8,
    "meta": {},
    "created_at": "2026-01-18T10:15:00Z"
  },
  {
    "id": "message-uuid-2",
    "session_id": "session-uuid-1",
    "role": "assistant",
    "content": "I can help you with billing questions.",
    "retrieved_chunk_ids": ["chunk-uuid-1"],
    "confidence_score": 0.45,
    "token_count": 8,
    "meta": {
      "model_version": "gemini-1.5-pro"
    },
    "created_at": "2026-01-18T10:15:30Z"
  }
]
```

**Use Case:**
- Fetch message history without full session details
- Useful for lazy loading or pagination
- Same messages returned as in session details endpoint

**Display:**
- Messages list ordered by creation time
- Can be used for infinite scroll or pagination

---

### 8.4 Agent Takeover

**Page:** `/dashboard/support/conversations/{session_id}` (Take Over button)

**Endpoint:** `POST /chat/sessions/{session_id}/takeover`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Request:**
```
No body required - uses authenticated user info
```

**Response:**
```json
{
  "id": "session-uuid-1",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_identifier": "customer@example.com",
  "status": "escalated",
  "escalation_reason": "Customer requested to speak with human agent",
  "escalated_at": "2026-01-18T10:30:00Z",
  "agent_id": "agent-uuid-1",
  "agent_joined_at": "2026-01-18T10:35:00Z",
  "confidence_scores": [0.45, 0.42, 0.38],
  "meta": {
    "user_agent": "Mozilla/5.0...",
    "ip_hash": "abc123..."
  },
  "last_message_at": "2026-01-18T10:35:00Z",
  "created_at": "2026-01-18T10:15:00Z",
  "updated_at": "2026-01-18T10:35:00Z"
}
```

**Frontend Action:**
- Click "Take Over" button
- Show loading state
- On success:
  - Update UI to show agent is now assigned
  - Enable message input box
  - Show system message: "You joined the conversation"
  - Refresh conversation view
- Show error if already assigned to another agent

**What Happens:**
1. Assigns session to the current agent (sets `agent_id` and `agent_joined_at`)
2. If ticket exists, assigns it to the agent and updates status to IN_PROGRESS
3. Creates system message: "{Agent Name} joined the conversation"
4. Broadcasts "agent_joined" notification to customer via WebSocket
5. Returns updated session with agent assignment

**Error Cases:**
- 404: Session not found
- 403: User doesn't have SUPPORT_AGENT or TENANT_ADMIN role
- 403: Session belongs to different tenant

---

### 8.5 Send Agent Message (HTTP Alternative)

**Page:** `/dashboard/support/conversations/{session_id}` (message input)

**Endpoint:** `POST /chat/sessions/{session_id}/agent-message`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Request:**
```json
{
  "message": "Hi! I'm here to help with your billing issue. Can you tell me more about what's happening?"
}
```

**Response:**
```json
{
  "id": "message-uuid-5",
  "session_id": "session-uuid-1",
  "role": "human_agent",
  "content": "Hi! I'm here to help with your billing issue. Can you tell me more about what's happening?",
  "retrieved_chunk_ids": null,
  "confidence_score": null,
  "token_count": null,
  "meta": {
    "agent_id": "agent-uuid-1",
    "agent_name": "Sarah Johnson"
  },
  "created_at": "2026-01-18T10:35:30Z"
}
```

**Use Cases:**
- Agents who prefer REST over WebSocket
- Automated agent responses
- Testing and debugging
- Simple integrations

**Frontend Action:**
- Submit message from input box
- Show message immediately in chat (optimistic UI)
- On success: Confirm message saved
- On error: Show error and allow retry

**What Happens:**
1. Creates message with role `human_agent`
2. Stores agent metadata (ID and name) in message
3. Broadcasts message to customer via WebSocket
4. Broadcasts to other agents via WebSocket (excluding sender)
5. Returns created message

**Note:** For real-time chat, WebSocket is recommended (see 8.6). This endpoint is useful for non-real-time scenarios or when WebSocket is not available.

---

### 8.6 WebSocket Real-Time Chat

**Page:** `/dashboard/support/conversations/{session_id}` (real-time chat)

**WebSocket Endpoint:** `wss://api.luranai.com/api/v1/ws/agent/{session_id}`

**Connection:**
```javascript
// Connect to agent WebSocket
const ws = new WebSocket(
  `wss://api.luranai.com/api/v1/ws/agent/${sessionId}?token=${tenantToken}`
);

ws.onopen = () => {
  console.log('Connected to session');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  // Handle different message types
  switch(data.type) {
    case 'customer_message':
      // New message from customer
      displayMessage({
        role: 'user',
        content: data.content,
        timestamp: data.timestamp
      });
      break;

    case 'agent_message':
      // Message from another agent
      displayMessage({
        role: 'human_agent',
        content: data.content,
        agent_name: data.agent_name,
        timestamp: data.timestamp
      });
      break;

    case 'agent_joined':
      // Another agent joined
      displaySystemMessage(`${data.agent_name} joined the conversation`);
      break;

    case 'agent_left':
      // Agent left the conversation
      displaySystemMessage(`${data.agent_name} left the conversation`);
      break;

    case 'session_resolved':
      // Session was resolved
      displaySystemMessage('Session resolved');
      disableMessageInput();
      break;
  }
};

// Send agent message
function sendAgentMessage(content) {
  ws.send(JSON.stringify({
    type: 'agent_message',
    content: content
  }));
}

ws.onclose = () => {
  console.log('Disconnected from session');
  // Handle reconnection if needed
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};
```

**Message Types (Received):**
- `customer_message` - New message from customer
- `agent_message` - Message from another agent in same session
- `agent_joined` - Another agent joined the conversation
- `agent_left` - Agent left the conversation
- `session_resolved` - Session was marked as resolved
- `connection_status` - Connection confirmation

**Message Types (Sent):**
- `agent_message` - Send message as agent
  ```json
  {
    "type": "agent_message",
    "content": "Your message here"
  }
  ```

**WebSocket Benefits:**
- Real-time bidirectional communication
- Instant message delivery
- No polling required
- Lower latency
- Live status updates

**Important Notes:**
1. Pass authentication token in query parameter: `?token={tenantToken}`
2. WebSocket automatically handles agent assignment checking
3. Messages are broadcasted to all connected parties (customer + agents)
4. Connection persists until closed by client or session ends
5. Auto-reconnect on connection loss (implement in frontend)

---

### 8.7 Customer WebSocket Real-Time Chat

**Widget Implementation:** Chat widget iframe real-time updates

**WebSocket Endpoint:** `wss://api.luranai.com/api/v1/ws/chat/{session_id}`

**Connection:**
```javascript
// Connect to customer WebSocket (for widget/chat UI)
const ws = new WebSocket(
  `wss://api.luranai.com/api/v1/ws/chat/${sessionId}?user_identifier=${customerEmail}`
);

ws.onopen = () => {
  console.log('Connected to chat');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  // Handle different message types
  switch(data.type) {
    case 'agent_message':
      // Message from agent
      displayMessage({
        role: 'human_agent',
        content: data.content,
        agent_name: data.agent_name,
        timestamp: data.timestamp
      });
      break;

    case 'agent_joined':
      // Agent joined the conversation
      displaySystemMessage(`${data.agent_name} joined the chat`);
      break;

    case 'agent_left':
      // Agent left the conversation
      displaySystemMessage(`${data.agent_name} left the chat`);
      break;

    case 'session_resolved':
      // Session was resolved
      displaySystemMessage('This conversation has been resolved');
      disableMessageInput();
      break;
  }
};

// Send customer message
function sendCustomerMessage(content) {
  ws.send(JSON.stringify({
    type: 'message',
    content: content
  }));
}

ws.onclose = () => {
  console.log('Disconnected from chat');
  // Handle reconnection if needed
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};
```

**Authentication:**
- **Query Parameter**: `user_identifier` (customer email or unique identifier)
- **Verification**: Server verifies `user_identifier` matches the session's `user_identifier`
- **No JWT Required**: Customers don't need JWT tokens

**Message Types (Received):**
- `agent_message` - Message from agent
- `agent_joined` - Agent joined the conversation
- `agent_left` - Agent left the conversation
- `session_resolved` - Session marked as resolved
- `connection_status` - Connection confirmation

**Message Types (Sent):**
- `message` - Send message as customer
  ```json
  {
    "type": "message",
    "content": "Customer message here"
  }
  ```

**Important Notes:**
1. Pass `user_identifier` in query parameter (must match session's user_identifier)
2. Customer WebSocket is for widget/chat UI real-time updates
3. Messages sent via WebSocket are saved to database automatically
4. Connection rejected if `user_identifier` doesn't match session
5. Customers can only connect to their own sessions

**For Complete WebSocket Documentation:**
See `/docs/WEBSOCKET_LIVE_CHAT_GUIDE.md` for:
- Full message format specifications
- Connection lifecycle details
- Error handling patterns
- Advanced WebSocket features
- Connection management best practices

---

## 9. Ticket Management

Complete CRUD operations for support tickets created from escalated conversations.

### 9.1 List Tickets

**Page:** `/dashboard/support/tickets`

**Endpoint:** `GET /tickets`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Query Parameters:**
```
?tenant_id=550e8400-e29b-41d4-a716-446655440000  (optional, defaults to current user's tenant)
?status=open                                      (optional filter)
?assigned_to_me=true                              (optional, show only my tickets)
?limit=100                                         (default 100, max 500)
?offset=0                                          (default 0, for pagination)
```

**Status Filter Values:**
- `open` - Newly created, unassigned
- `in_progress` - Assigned and being worked on
- `resolved` - Solution provided
- `closed` - Finalized and archived

**Example Request:**
```
GET /tickets?status=open&limit=50&offset=0
```

**Response:**
```json
{
  "tickets": [
    {
      "id": "ticket-uuid-1",
      "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
      "session_id": "session-uuid-1",
      "ticket_number": "TKT-2026-0001",
      "status": "open",
      "priority": "high",
      "summary": "Customer unable to access billing dashboard - 403 error",
      "assigned_to": null,
      "assigned_agent": null,
      "resolution_notes": null,
      "resolved_at": null,
      "created_at": "2026-01-18T10:30:00Z",
      "updated_at": "2026-01-18T10:30:00Z"
    },
    {
      "id": "ticket-uuid-2",
      "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
      "session_id": "session-uuid-2",
      "ticket_number": "TKT-2026-0002",
      "status": "in_progress",
      "priority": "medium",
      "summary": "Payment method update failing with validation error",
      "assigned_to": "agent-uuid-1",
      "assigned_agent": {
        "id": "agent-uuid-1",
        "email": "john.doe@acme.com",
        "full_name": "John Doe",
        "role": "SUPPORT_AGENT"
      },
      "resolution_notes": null,
      "resolved_at": null,
      "created_at": "2026-01-18T09:45:00Z",
      "updated_at": "2026-01-18T10:00:00Z"
    }
  ],
  "total": 47,
  "page": 1,
  "page_size": 100
}
```

**Ticket Priority Values:**
- `low` - Low priority
- `medium` - Medium priority
- `high` - High priority
- `urgent` - Urgent attention needed

**Display:**
- Table with columns: Ticket #, Summary, Status, Priority, Assigned To, Created, Actions
- For "Assigned To" column: Use `assigned_agent.full_name` if available, otherwise show "Unassigned"
- Filter dropdowns: Status, Priority, Assigned to me
- Search by ticket number or summary
- Status badges with colors:
  - Open: Blue
  - In Progress: Yellow
  - Resolved: Green
  - Closed: Gray
- Priority badges with colors:
  - Low: Gray
  - Medium: Blue
  - High: Orange
  - Urgent: Red
- Pagination controls
- Actions: View Details, Assign, Resolve (based on status and role)

**What Happens:**
- Returns paginated list of tickets for the tenant
- Automatically filters by authenticated user's tenant
- Can filter by status, assignment, or show only user's assigned tickets
- Ordered by creation time (newest first)

---

### 9.2 Get Ticket Details

**Page:** `/dashboard/support/tickets/{ticket_id}`

**Endpoint:** `GET /tickets/{ticket_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Response:**
```json
{
  "id": "ticket-uuid-1",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "session-uuid-1",
  "ticket_number": "TKT-2026-0001",
  "status": "in_progress",
  "priority": "high",
  "summary": "Customer unable to access billing dashboard - 403 error",
  "assigned_to": "agent-uuid-1",
  "assigned_agent": {
    "id": "agent-uuid-1",
    "email": "john.doe@acme.com",
    "full_name": "John Doe",
    "role": "SUPPORT_AGENT"
  },
  "resolution_notes": null,
  "resolved_at": null,
  "created_at": "2026-01-18T10:30:00Z",
  "updated_at": "2026-01-18T11:15:00Z"
}
```

**Note:** The `assigned_agent` object is populated when the ticket is assigned to an agent. If the ticket is unassigned, both `assigned_to` and `assigned_agent` will be `null`. The `assigned_to` field is kept for backwards compatibility.

**Display:**
- Full ticket details card showing all fields
- Link to associated conversation: "View Conversation" button → `/dashboard/support/conversations/{session_id}`
- Assigned agent name from `assigned_agent.full_name` (no need to fetch separately)
- Status and priority badges
- Action buttons based on status:
  - If `open`: "Assign to Me", "Assign to Other Agent", "Update Priority"
  - If `in_progress`: "Add Notes", "Resolve", "Update Priority"
  - If `resolved`: "Close Ticket", "Reopen"
  - If `closed`: "View Only"
- Timeline showing:
  - Created at
  - Assigned at (if applicable)
  - Resolved at (if applicable)
  - Updated at

**What Happens:**
- Retrieves full ticket information
- Tenant access verified automatically
- Can be used to display detailed ticket view

---

### 9.3 Create Ticket Manually

**Page:** `/dashboard/support/tickets/create`

**Endpoint:** `POST /tickets`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Request:**
```json
{
  "session_id": "session-uuid-1",
  "summary": "Customer reporting payment processing delays",
  "priority": "high"
}
```

**Priority Options:**
- `low` - Low priority (default if not specified)
- `medium` - Medium priority
- `high` - High priority
- `urgent` - Urgent attention needed

**Response:**
```json
{
  "id": "ticket-uuid-3",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "session-uuid-1",
  "ticket_number": "TKT-2026-0003",
  "status": "open",
  "priority": "high",
  "summary": "Customer reporting payment processing delays",
  "assigned_to": null,
  "resolution_notes": null,
  "resolved_at": null,
  "created_at": "2026-01-18T11:00:00Z",
  "updated_at": "2026-01-18T11:00:00Z"
}
```

**Use Case:**
- Create tickets manually for conversations that need tracking
- Create tickets for issues reported via other channels (phone, email)
- Must have an associated conversation session

**Frontend Action:**
- Show success message: "Ticket TKT-2026-0003 created successfully"
- Redirect to ticket details page or tickets list
- Optionally: "Assign to yourself?" prompt

**What Happens:**
1. Validates session exists and belongs to tenant
2. Generates unique ticket number (format: TKT-YYYY-NNNN)
3. Creates ticket with status `open`
4. Tenant ID auto-populated from session
5. Returns created ticket

---

### 9.4 Assign Ticket

**Page:** `/dashboard/support/tickets/{ticket_id}` (Assign button)

**Endpoint:** `POST /tickets/{ticket_id}/assign`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```json
{
  "assigned_to": "agent-uuid-1"
}
```

**Response:**
```json
{
  "id": "ticket-uuid-1",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "session-uuid-1",
  "ticket_number": "TKT-2026-0001",
  "status": "in_progress",
  "priority": "high",
  "summary": "Customer unable to access billing dashboard - 403 error",
  "assigned_to": "agent-uuid-1",
  "resolution_notes": null,
  "resolved_at": null,
  "created_at": "2026-01-18T10:30:00Z",
  "updated_at": "2026-01-18T11:20:00Z"
}
```

**Frontend Action:**
- Show agent selector dropdown (list of support agents from `/users` endpoint)
- On assign:
  - Update UI to show assigned agent
  - Update status badge to "In Progress"
  - Show success: "Ticket assigned to {Agent Name}"

**What Happens:**
1. Validates assigned user exists and belongs to same tenant
2. Validates assigned user has SUPPORT_AGENT or TENANT_ADMIN role
3. Updates `assigned_to` field
4. Automatically updates status from `open` to `in_progress` if currently open
5. Returns updated ticket

**Quick Assign:**
- "Assign to Me" button: Uses current user's ID
- "Assign to Other" dropdown: Select from team members

---

### 9.5 Update Ticket

**Page:** `/dashboard/support/tickets/{ticket_id}/edit`

**Endpoint:** `PUT /tickets/{ticket_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:** (all fields optional)
```json
{
  "status": "in_progress",
  "priority": "urgent",
  "assigned_to": "agent-uuid-2",
  "resolution_notes": "Investigating billing service logs"
}
```

**Updatable Fields:**
- `status` - Ticket status (open, in_progress, resolved, closed)
- `priority` - Priority level (low, medium, high, urgent)
- `assigned_to` - Assigned agent UUID (or null to unassign)
- `resolution_notes` - Working notes (text field)

**Response:**
```json
{
  "id": "ticket-uuid-1",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "session-uuid-1",
  "ticket_number": "TKT-2026-0001",
  "status": "in_progress",
  "priority": "urgent",
  "summary": "Customer unable to access billing dashboard - 403 error",
  "assigned_to": "agent-uuid-2",
  "resolution_notes": "Investigating billing service logs",
  "resolved_at": null,
  "created_at": "2026-01-18T10:30:00Z",
  "updated_at": "2026-01-18T11:45:00Z"
}
```

**Use Case:**
- Update ticket priority based on urgency
- Change assignment to different agent
- Update status manually
- Add working notes during investigation

**Frontend Action:**
- Edit form with all updateable fields
- Validation:
  - Cannot set status to `resolved` without resolution_notes (use resolve endpoint instead)
  - assigned_to must be valid user UUID from same tenant
- Show success message on update
- Refresh ticket details

---

### 9.6 Resolve Ticket

**Page:** `/dashboard/support/tickets/{ticket_id}` (Resolve button)

**Endpoint:** `POST /tickets/{ticket_id}/resolve`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** SUPPORT_AGENT or TENANT_ADMIN

**Request:**
```json
{
  "resolution_notes": "Issue was caused by expired session token. Customer successfully logged in after clearing cookies and cache. No further action required."
}
```

**Response:**
```json
{
  "id": "ticket-uuid-1",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "session-uuid-1",
  "ticket_number": "TKT-2026-0001",
  "status": "resolved",
  "priority": "high",
  "summary": "Customer unable to access billing dashboard - 403 error",
  "assigned_to": "agent-uuid-1",
  "resolution_notes": "Issue was caused by expired session token. Customer successfully logged in after clearing cookies and cache. No further action required.",
  "resolved_at": "2026-01-18T12:00:00Z",
  "created_at": "2026-01-18T10:30:00Z",
  "updated_at": "2026-01-18T12:00:00Z"
}
```

**Frontend Action:**
- Show "Resolve Ticket" modal with textarea for resolution notes
- Required field: Resolution notes (minimum 1 character)
- On submit:
  - Update status badge to "Resolved"
  - Show resolved timestamp
  - Display resolution notes
  - Show success: "Ticket resolved successfully"
  - Enable "Close Ticket" button

**What Happens:**
1. Validates resolution_notes is provided
2. Updates ticket status to `resolved`
3. Sets `resolution_notes` field
4. Sets `resolved_at` timestamp to current time
5. Returns updated ticket

**Note:** Only support agents or admins can resolve tickets. Resolution notes are required and should describe what was done to resolve the issue.

---

### 9.7 Close Ticket

**Page:** `/dashboard/support/tickets/{ticket_id}` (Close button)

**Endpoint:** `POST /tickets/{ticket_id}/close`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```
No body required
```

**Response:**
```json
{
  "id": "ticket-uuid-1",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "session-uuid-1",
  "ticket_number": "TKT-2026-0001",
  "status": "closed",
  "priority": "high",
  "summary": "Customer unable to access billing dashboard - 403 error",
  "assigned_to": "agent-uuid-1",
  "resolution_notes": "Issue was caused by expired session token. Customer successfully logged in after clearing cookies and cache. No further action required.",
  "resolved_at": "2026-01-18T12:00:00Z",
  "created_at": "2026-01-18T10:30:00Z",
  "updated_at": "2026-01-18T12:15:00Z"
}
```

**Frontend Action:**
- Show confirmation dialog: "Close this ticket? This marks it as finalized and archived."
- On confirm:
  - Update status badge to "Closed"
  - Make ticket read-only (disable all edit buttons)
  - Show success: "Ticket closed"
- Only show "Close" button if status is `resolved`

**What Happens:**
1. Validates ticket is in `resolved` status
2. Updates status to `closed`
3. Returns updated ticket

**Workflow:**
Tickets must be resolved before they can be closed. This two-step process ensures:
- Resolution notes are always captured
- Proper documentation of ticket lifecycle
- Separation between "solution provided" and "case finalized"

---

### 9.8 Get Unassigned Tickets

**Page:** `/dashboard/support/tickets` (Unassigned filter)

**Endpoint:** `GET /tickets/unassigned`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Query Parameters:**
```
?tenant_id=550e8400-e29b-41d4-a716-446655440000  (optional, defaults to current user's tenant)
```

**Response:**
```json
[
  {
    "id": "ticket-uuid-1",
    "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
    "session_id": "session-uuid-1",
    "ticket_number": "TKT-2026-0001",
    "status": "open",
    "priority": "high",
    "summary": "Customer unable to access billing dashboard - 403 error",
    "assigned_to": null,
    "resolution_notes": null,
    "resolved_at": null,
    "created_at": "2026-01-18T10:30:00Z",
    "updated_at": "2026-01-18T10:30:00Z"
  },
  {
    "id": "ticket-uuid-3",
    "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
    "session_id": "session-uuid-3",
    "ticket_number": "TKT-2026-0003",
    "status": "open",
    "priority": "urgent",
    "summary": "Payment processing failure requiring immediate attention",
    "assigned_to": null,
    "resolution_notes": null,
    "resolved_at": null,
    "created_at": "2026-01-18T11:00:00Z",
    "updated_at": "2026-01-18T11:00:00Z"
  }
]
```

**Display:**
- Table showing only tickets with `assigned_to: null`
- Sorted by priority (urgent → high → medium → low), then by creation time
- Prominent "Assign to Me" button for each ticket
- Badge showing count: "12 Unassigned Tickets"

**Use Case:**
- Quick view of tickets needing assignment
- Admins can see workload distribution
- Agents can self-assign from unassigned pool

**What Happens:**
- Returns tickets where `assigned_to` is null
- Filtered by tenant automatically
- Ordered by priority and creation time

---

## 10. Analytics Dashboard

The Analytics Dashboard provides comprehensive metrics and insights about conversations, tickets, and agent performance. All analytics endpoints require the `can_access_analytics` capability to be enabled for your tenant.

**Important Requirements:**
- User must have `can_access_analytics` capability enabled in tenant settings
- All endpoints require authentication (`Bearer {tenant_token}`)
- Date ranges default to last 30 days if not specified
- All data is automatically filtered by tenant (cannot access other tenants' data)

---

### 10.1 Get Overview Metrics

**Page:** `/dashboard/analytics`

**Endpoint:** `GET /analytics/overview`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Query Parameters:**
```
?start_date=2026-01-01T00:00:00Z
?end_date=2026-01-31T23:59:59Z
```

**Request Example:**
```
GET /analytics/overview?start_date=2026-01-01T00:00:00Z&end_date=2026-01-31T23:59:59Z
```

**Response:**
```json
{
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_conversations": 1250,
  "active_conversations": 45,
  "escalated_conversations": 125,
  "resolved_conversations": 980,
  "abandoned_conversations": 100,
  "escalation_rate": 10.0,
  "avg_confidence_score": 0.75,

  "total_tickets": 125,
  "open_tickets": 15,
  "in_progress_tickets": 25,
  "resolved_tickets": 80,
  "closed_tickets": 5,
  "unassigned_tickets": 8,
  "avg_resolution_time_hours": 4.5,

  "total_agents": 10,
  "active_agents": 7
}
```

**Field Explanations:**

**Conversation Metrics:**
- `total_conversations` - Total conversations in period
- `active_conversations` - Currently active AI conversations
- `escalated_conversations` - Conversations escalated to human agents
- `resolved_conversations` - Successfully resolved conversations
- `abandoned_conversations` - Conversations where customer stopped responding
- `escalation_rate` - Percentage of conversations escalated (escalated / total × 100)
- `avg_confidence_score` - Average AI confidence score (0.0 to 1.0)

**Ticket Metrics:**
- `total_tickets` - Total tickets created in period
- `open_tickets` - Tickets with OPEN status
- `in_progress_tickets` - Tickets being actively worked on
- `resolved_tickets` - Resolved tickets
- `closed_tickets` - Closed tickets
- `unassigned_tickets` - Currently unassigned tickets
- `avg_resolution_time_hours` - Average time to resolve tickets (in hours)

**Agent Metrics:**
- `total_agents` - Total agents in tenant
- `active_agents` - Agents with currently assigned tickets

**Display:**
- Dashboard cards for each major metric
- Percentage indicators for escalation rate
- Time-based charts comparing periods
- Color coding (green for good metrics, yellow for attention needed, red for issues)

**Error Responses:**
- `403` - Analytics access not enabled for tenant
- `400` - Invalid date range (end_date before start_date)
- `401` - Unauthorized (invalid or missing token)

---

### 10.2 Get Conversation Analytics

**Page:** `/dashboard/analytics/conversations`

**Endpoint:** `GET /analytics/conversations`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Query Parameters:**
```
?start_date=2026-01-01T00:00:00Z
?end_date=2026-01-31T23:59:59Z
?status_filter=escalated
```

**Status Filter Values:**
- `active` - Active AI conversations
- `escalated` - Escalated to human
- `resolved` - Resolved conversations
- `abandoned` - Abandoned by customer

**Response:**
```json
{
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_conversations": 1250,
  "conversations_by_status": {
    "active": 45,
    "escalated": 125,
    "resolved": 980,
    "abandoned": 100
  },
  "escalation_rate": 10.0,
  "avg_session_duration_minutes": 12.5,
  "avg_messages_per_session": 8.3,
  "avg_confidence_score": 0.75,
  "confidence_distribution": {
    "0-25%": 50,
    "25-50%": 200,
    "50-75%": 500,
    "75-100%": 500
  },
  "top_escalation_reasons": [
    {
      "reason": "Customer requested human agent",
      "count": 45
    },
    {
      "reason": "Low confidence score",
      "count": 35
    },
    {
      "reason": "Complex technical issue",
      "count": 25
    }
  ]
}
```

**Field Explanations:**
- `conversations_by_status` - Count of conversations grouped by status
- `avg_session_duration_minutes` - Average conversation length
- `avg_messages_per_session` - Average number of messages per conversation
- `confidence_distribution` - Distribution of AI confidence scores in ranges
- `top_escalation_reasons` - Most common reasons for escalation (up to 5)

**Display:**
- Pie chart for status distribution
- Bar chart for confidence distribution
- List of top escalation reasons with counts
- Line chart for session duration trends

---

### 10.3 Get Ticket Analytics

**Page:** `/dashboard/analytics/tickets`

**Endpoint:** `GET /analytics/tickets`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Query Parameters:**
```
?start_date=2026-01-01T00:00:00Z
?end_date=2026-01-31T23:59:59Z
?status_filter=open
?priority_filter=high
```

**Status Filter Values:**
- `open` - Open tickets
- `in_progress` - In progress
- `resolved` - Resolved
- `closed` - Closed

**Priority Filter Values:**
- `urgent` - Urgent priority
- `high` - High priority
- `medium` - Medium priority
- `low` - Low priority

**Response:**
```json
{
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_tickets": 125,
  "tickets_by_status": {
    "open": 15,
    "in_progress": 25,
    "resolved": 80,
    "closed": 5
  },
  "tickets_by_priority": {
    "urgent": 10,
    "high": 30,
    "medium": 60,
    "low": 25
  },
  "avg_resolution_time_hours": 4.5,
  "resolution_time_by_priority": {
    "urgent": 1.5,
    "high": 3.0,
    "medium": 5.5,
    "low": 8.0
  },
  "unassigned_tickets": 8,
  "tickets_created": 125,
  "tickets_resolved": 85
}
```

**Field Explanations:**
- `tickets_by_status` - Count of tickets grouped by status
- `tickets_by_priority` - Count of tickets grouped by priority level
- `resolution_time_by_priority` - Average resolution time for each priority level
- `tickets_created` - Total tickets created in period
- `tickets_resolved` - Total tickets resolved in period

**Display:**
- Status distribution pie chart
- Priority distribution bar chart
- Resolution time comparison chart by priority
- Tickets created vs resolved trend line

---

### 10.4 Get All Agents Performance (Admin Only)

**Page:** `/dashboard/analytics/agents`

**Endpoint:** `GET /analytics/agents`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Query Parameters:**
```
?start_date=2026-01-01T00:00:00Z
?end_date=2026-01-31T23:59:59Z
```

**Response:**
```json
[
  {
    "agent_id": "agent-uuid-1",
    "agent_name": "Sarah Johnson",
    "email": "sarah@acmecorp.com",
    "current_assigned_tickets": 5,
    "tickets_by_status": {
      "open": 2,
      "in_progress": 3,
      "resolved": 45
    },
    "total_tickets_resolved": 245
  },
  {
    "agent_id": "agent-uuid-2",
    "agent_name": "John Smith",
    "email": "john@acmecorp.com",
    "current_assigned_tickets": 3,
    "tickets_by_status": {
      "open": 1,
      "in_progress": 2,
      "resolved": 32
    },
    "total_tickets_resolved": 189
  }
]
```

**Field Explanations:**
- `current_assigned_tickets` - Currently assigned tickets (all statuses)
- `tickets_by_status` - Breakdown of agent's tickets by status
- `total_tickets_resolved` - All-time resolved tickets for this agent

**Display:**
- Table with columns: Name, Email, Assigned, In Progress, Resolved, Total Resolved
- Sortable by any column
- Click row to view detailed agent performance

**Error Responses:**
- `403` - Not a tenant admin (only admins can view all agents)

---

### 10.5 Get Individual Agent Performance

**Page:** `/dashboard/analytics/agents/{agent_id}`

**Endpoint:** `GET /analytics/agents/{agent_id}`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Access Control:**
- Agents can view their own metrics
- Admins can view any agent's metrics

**Query Parameters:**
```
?start_date=2026-01-01T00:00:00Z
?end_date=2026-01-31T23:59:59Z
```

**Response:**
```json
{
  "agent_id": "agent-uuid-1",
  "agent_name": "Sarah Johnson",
  "agent_email": "sarah@acmecorp.com",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_tickets_assigned": 50,
  "tickets_resolved": 45,
  "tickets_in_progress": 3,
  "avg_resolution_time_hours": 3.5,
  "conversations_handled": 48,
  "avg_response_time_minutes": 2.5
}
```

**Field Explanations:**
- `total_tickets_assigned` - Tickets assigned to this agent in period
- `tickets_resolved` - Tickets resolved by agent in period
- `tickets_in_progress` - Currently in progress tickets
- `avg_resolution_time_hours` - Agent's average resolution time
- `conversations_handled` - Escalated conversations handled by agent
- `avg_response_time_minutes` - Average time to first response

**Display:**
- Agent profile card with photo and name
- Performance metrics cards
- Comparison to team average
- Resolution time trend chart
- Recent tickets handled table

**Error Responses:**
- `403` - Agent trying to view another agent's metrics (only own or admin)
- `404` - Agent not found

---

### 10.6 Get Conversation Trends

**Page:** `/dashboard/analytics/trends`

**Endpoint:** `GET /analytics/trends/conversations`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Query Parameters:**
```
?start_date=2026-01-01T00:00:00Z
?end_date=2026-01-31T23:59:59Z
?granularity=daily
```

**Granularity Values:**
- `daily` - Data grouped by day
- `weekly` - Data grouped by week
- `monthly` - Data grouped by month

**Response:**
```json
{
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "metric_name": "conversation_count",
  "granularity": "daily",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",
  "data_points": [
    {
      "timestamp": "2026-01-01T00:00:00Z",
      "value": 42.0,
      "meta": null
    },
    {
      "timestamp": "2026-01-02T00:00:00Z",
      "value": 38.0,
      "meta": null
    },
    {
      "timestamp": "2026-01-03T00:00:00Z",
      "value": 45.0,
      "meta": null
    }
  ]
}
```

**Field Explanations:**
- `metric_name` - Type of metric ("conversation_count")
- `data_points` - Array of timestamp-value pairs
- `timestamp` - Start of the time period
- `value` - Count of conversations in that period

**Display:**
- Line chart with time on X-axis and count on Y-axis
- Interactive hover to show exact values
- Zoom controls for date range
- Export to CSV button

**Error Responses:**
- `422` - Invalid granularity value (must be daily, weekly, or monthly)

---

### 10.7 Get Ticket Trends

**Page:** `/dashboard/analytics/trends`

**Endpoint:** `GET /analytics/trends/tickets`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Query Parameters:**
```
?start_date=2026-01-01T00:00:00Z
?end_date=2026-01-31T23:59:59Z
?granularity=daily
```

**Granularity Values:**
- `daily` - Data grouped by day
- `weekly` - Data grouped by week
- `monthly` - Data grouped by month

**Response:**
```json
{
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "metric_name": "ticket_count",
  "granularity": "daily",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",
  "data_points": [
    {
      "timestamp": "2026-01-01T00:00:00Z",
      "value": 5.0,
      "meta": null
    },
    {
      "timestamp": "2026-01-02T00:00:00Z",
      "value": 3.0,
      "meta": null
    },
    {
      "timestamp": "2026-01-03T00:00:00Z",
      "value": 7.0,
      "meta": null
    }
  ]
}
```

**Display:**
- Line chart showing ticket volume over time
- Overlay with created vs resolved tickets
- Trend line showing increase/decrease
- Period comparison (current vs previous period)

---

### 10.8 Get Knowledge Usage Analytics

**Page:** `/dashboard/analytics/knowledge`

**Endpoint:** `GET /analytics/knowledge`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Query Parameters:**
```
?start_date=2026-01-01T00:00:00Z
?end_date=2026-01-31T23:59:59Z
```

**Response:**
```json
{
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_knowledge_chunks": 450,
  "chunks_used_in_conversations": 320,
  "usage_rate": 71.1,
  "top_used_chunks": [
    {
      "chunk_id": "chunk-uuid-1",
      "source_id": "source-uuid-1",
      "usage_count": 145
    },
    {
      "chunk_id": "chunk-uuid-2",
      "source_id": "source-uuid-2",
      "usage_count": 98
    }
  ],
  "avg_chunks_per_conversation": 2.8,
  "knowledge_effectiveness_score": 0.85
}
```

**Field Explanations:**
- `total_knowledge_chunks` - Total knowledge chunks in your knowledge base
- `chunks_used_in_conversations` - Unique chunks referenced in AI responses
- `usage_rate` - Percentage of knowledge base being used
- `top_used_chunks` - Most frequently referenced knowledge chunks
- `avg_chunks_per_conversation` - Average knowledge chunks used per conversation
- `knowledge_effectiveness_score` - Effectiveness ratio (resolved / escalated when using knowledge)

**Display:**
- Knowledge base utilization gauge
- List of most-used knowledge chunks with links to view content
- Effectiveness score indicator
- Recommendations for unused knowledge

**Note:** Knowledge analytics helps you:
- Identify which documentation is most valuable
- Find gaps in your knowledge base
- Optimize knowledge base content
- Remove unused or outdated information

---

### Common Analytics Patterns

**Date Range Selection:**
All analytics endpoints support optional date ranges:
- If not provided: Defaults to last 30 days
- `start_date` must be before `end_date`
- Dates should be in ISO 8601 format: `2026-01-15T10:30:00Z`

**Permission Checking:**
All analytics endpoints verify:
1. User is authenticated (valid JWT token)
2. Tenant has `can_access_analytics` capability enabled
3. User belongs to the tenant they're querying

**Error Handling:**
```json
// Analytics not enabled
{
  "detail": "Analytics access not enabled for this tenant"
}

// Invalid date range
{
  "detail": "End date must be after start date"
}

// Unauthorized access
{
  "detail": "Could not validate credentials"
}
```

**Frontend Best Practices:**
1. Cache analytics data for 5-10 minutes to reduce API calls
2. Show loading skeletons while fetching data
3. Provide date range picker with common presets (Last 7 days, Last 30 days, This month, etc.)
4. Export functionality for reports (CSV, PDF)
5. Real-time updates using periodic polling (every 60 seconds)
6. Responsive charts that work on mobile devices

**Dashboard Layout Recommendations:**
```
┌─────────────────────────────────────────────────────┐
│  Analytics Dashboard                    [Date Range] │
├─────────────────────────────────────────────────────┤
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐         │
│  │   1,250  │  │   125    │  │   10.0%  │         │
│  │Conversa- │  │ Tickets  │  │Escalation│         │
│  │  tions   │  │          │  │   Rate   │         │
│  └──────────┘  └──────────┘  └──────────┘         │
│                                                      │
│  ┌────────────────────────────────────────────┐    │
│  │  Conversation Volume Over Time             │    │
│  │  [Line Chart]                              │    │
│  └────────────────────────────────────────────┘    │
│                                                      │
│  ┌────────────────────┐  ┌────────────────────┐   │
│  │ Tickets by Status  │  │ Agent Performance  │   │
│  │ [Pie Chart]        │  │ [Table]            │   │
│  └────────────────────┘  └────────────────────┘   │
│                                                      │
└─────────────────────────────────────────────────────┘
```

---

## 11. Review & Rating System

The review system allows customers to rate their chat experience and enables companies to provide feedback about the Luran platform.

### 11.1 Submit Session Rating (Public)

**Page:** Widget chat interface (after conversation ends)

**Endpoint:** `POST /reviews/session`

**Authentication:** None required (public endpoint)

**Use Case:** After a customer completes a chat session, prompt them to rate their experience with the AI assistant. This helps companies identify knowledge gaps and improve response quality.

**Request:**
```json
{
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "user_identifier": "customer@example.com",
  "score": 5,
  "comment": "Very helpful! Got my question answered quickly.",
  "meta": {
    "ai_helpful": true,
    "issue_resolved": true
  }
}
```

**Request Fields:**
- `session_id` (UUID, required): The conversation session being rated
- `user_identifier` (string, required): Must match the session's user_identifier (for verification)
- `score` (integer, required): Rating from 1-5 (1=Poor, 2=Fair, 3=Good, 4=Very Good, 5=Excellent)
- `comment` (string, optional): Optional feedback comment (max 2000 chars)
- `meta` (object, optional): Additional context about the experience

**Response (201 Created):**
```json
{
  "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "tenant_id": "1fa85f64-5717-4562-b3fc-2c963f66afa6",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "user_identifier": "customer@example.com",
  "score": 5,
  "comment": "Very helpful! Got my question answered quickly.",
  "meta": {
    "ai_helpful": true,
    "issue_resolved": true
  },
  "created_at": "2026-01-23T15:30:00Z",
  "updated_at": "2026-01-23T15:30:00Z"
}
```

**Error Responses:**

```json
// Session not found (400)
{
  "detail": "Session not found"
}

// User identifier mismatch (400)
{
  "detail": "User identifier does not match session"
}

// Duplicate rating (409)
{
  "detail": "Rating already exists for this session"
}

// Invalid score (422)
{
  "detail": [
    {
      "loc": ["body", "score"],
      "msg": "ensure this value is greater than or equal to 1",
      "type": "value_error.number.not_ge"
    }
  ]
}
```

**Frontend Implementation:**
1. After conversation ends or when customer indicates satisfaction, show rating prompt
2. Display 5-star rating UI with optional comment field
3. Submit rating with session_id and user_identifier
4. Handle duplicate rating (409) gracefully - show "Already rated" message
5. Show thank you message after successful submission

---

### 11.2 List Session Ratings

**Page:** `/dashboard/analytics/ratings` (tenant users) or `/platform-admin/ratings` (platform admins)

**Endpoint:** `GET /reviews/session`

**Authentication:** Required (Any tenant user OR Platform admin)

**Headers:**
```
Authorization: Bearer {tenant_token}
OR
Authorization: Bearer {platform_admin_token}
```

**Access Control:**
- **Tenant users** (any role: TENANT_ADMIN, SUPPORT_AGENT, VIEWER): View ratings for their own tenant only
- **Platform admins**: View ALL ratings across ALL tenants (system-wide)

**Use Case:** View customer ratings to identify knowledge gaps, track satisfaction trends, and improve AI response quality.

**Query Parameters:**
- `session_id` (UUID, optional): Filter by specific session
- `min_score` (integer, optional): Minimum rating score (1-5)
- `max_score` (integer, optional): Maximum rating score (1-5)
- `start_date` (ISO 8601, optional): Filter from date
- `end_date` (ISO 8601, optional): Filter to date
- `limit` (integer, optional): Max results per page (1-100, default: 50)
- `offset` (integer, optional): Results offset for pagination (default: 0)

**Example Request (Tenant User):**
```
GET /reviews/session?min_score=1&max_score=3&limit=20&offset=0
Authorization: Bearer {tenant_token}
```
Returns ratings for the authenticated tenant only.

**Example Request (Platform Admin):**
```
GET /reviews/session?min_score=1&max_score=3&limit=20&offset=0
Authorization: Bearer {platform_admin_token}
```
Returns ratings across ALL tenants (system-wide).

**Response (200 OK):**
```json
{
  "ratings": [
    {
      "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
      "tenant_id": "1fa85f64-5717-4562-b3fc-2c963f66afa6",
      "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "user_identifier": "customer@example.com",
      "score": 2,
      "comment": "AI couldn't answer my question about pricing.",
      "meta": {
        "ai_helpful": false,
        "issue_resolved": false
      },
      "created_at": "2026-01-23T15:30:00Z",
      "updated_at": "2026-01-23T15:30:00Z"
    },
    {
      "id": "8d0e7780-8536-41ef-a34c-3d08f2e91bf8",
      "tenant_id": "1fa85f64-5717-4562-b3fc-2c963f66afa6",
      "session_id": "4fb85f64-5717-4562-b3fc-2c963f66afa7",
      "user_identifier": "user@company.com",
      "score": 5,
      "comment": "Great experience!",
      "meta": null,
      "created_at": "2026-01-23T14:15:00Z",
      "updated_at": "2026-01-23T14:15:00Z"
    }
  ],
  "total": 125,
  "average_score": 4.2,
  "page": 1,
  "page_size": 50
}
```

**Response Fields:**
- `ratings` (array): List of rating objects
- `total` (integer): Total number of ratings matching filters
- `average_score` (float, nullable): Average rating score across filtered results
- `page` (integer): Current page number
- `page_size` (integer): Number of results per page

**Important Notes:**
- **Tenant users**: Response includes only ratings for their tenant (filtered by `tenant_id`)
- **Platform admins**: Response includes ratings from ALL tenants (system-wide view). The `tenant_id` field will vary across ratings.

**Frontend Best Practices (Tenant Users):**
1. Display average score prominently with visual indicator (e.g., stars)
2. Show score distribution chart (how many 1-star, 2-star, etc.)
3. Filter by score range to quickly find low ratings
4. Link ratings to conversation sessions for context
5. Export ratings data for offline analysis
6. Show trends over time (average score by day/week/month)

**Additional Best Practices (Platform Admins):**
7. **Display tenant information**: Show company name alongside each rating (fetch from `tenant_id`)
8. **Add tenant filter**: Dropdown to filter by specific company
9. **Cross-tenant analytics**: Compare satisfaction scores across different tenants
10. **Identify struggling tenants**: Highlight companies with low average scores for proactive support

**Dashboard Widget Example:**
```
┌─────────────────────────────────────────┐
│  Customer Satisfaction                  │
├─────────────────────────────────────────┤
│  Average: ★★★★☆ 4.2 (125 ratings)      │
│                                         │
│  Distribution:                          │
│  ★★★★★  85  ████████████████████  68%  │
│  ★★★★☆  25  ███████              20%  │
│  ★★★☆☆   8  ███                   6%  │
│  ★★☆☆☆   5  ██                    4%  │
│  ★☆☆☆☆   2  █                     2%  │
│                                         │
│  [View Low Ratings] [Export Data]      │
└─────────────────────────────────────────┘
```

---

### 11.3 Submit Platform Feedback

**Page:** `/dashboard/feedback` or feedback modal in navigation

**Endpoint:** `POST /reviews/platform`

**Authentication:** Required (Any tenant user)

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Use Case:** Allow companies to provide feedback about Luran platform, report bugs, request features, or rate their overall satisfaction.

**Request:**
```json
{
  "category": "feature_request",
  "title": "Add dark mode to dashboard",
  "description": "It would be great to have a dark mode option for the dashboard. Working late at night with the bright interface is hard on the eyes.",
  "satisfaction_score": null,
  "meta": {
    "priority": "medium",
    "affected_features": ["dashboard", "ui"]
  }
}
```

**Request Fields:**
- `category` (enum, required): Type of feedback
  - `feature_request`: Request new features
  - `bug_report`: Report bugs or issues
  - `performance`: Performance-related feedback
  - `platform_satisfaction`: Rate overall platform satisfaction (requires satisfaction_score)
  - `documentation`: Documentation feedback
  - `pricing`: Pricing-related feedback
  - `general`: General feedback
- `title` (string, required): Feedback title/summary (max 500 chars)
- `description` (string, required): Detailed feedback description (max 10000 chars)
- `satisfaction_score` (integer, optional): 1-5 rating (only for platform_satisfaction category)
- `meta` (object, optional): Additional context

**Response (201 Created):**
```json
{
  "id": "9e1e8891-9647-42fg-b45d-4e19g3f02cf9",
  "tenant_id": "1fa85f64-5717-4562-b3fc-2c963f66afa6",
  "category": "feature_request",
  "satisfaction_score": null,
  "title": "Add dark mode to dashboard",
  "description": "It would be great to have a dark mode option for the dashboard. Working late at night with the bright interface is hard on the eyes.",
  "status": "open",
  "submitted_by": "2ga85f64-5717-4562-b3fc-2c963f66afa7",
  "meta": {
    "priority": "medium",
    "affected_features": ["dashboard", "ui"]
  },
  "created_at": "2026-01-23T16:00:00Z",
  "updated_at": "2026-01-23T16:00:00Z"
}
```

**Response Fields:**
- `status` (string): Feedback status (open, acknowledged, in_progress, resolved, closed)
- `submitted_by` (UUID): User ID who submitted the feedback

**Example - Platform Satisfaction Rating:**
```json
{
  "category": "platform_satisfaction",
  "title": "Very satisfied with Luran",
  "description": "The platform has been great for our customer support. Easy to set up and the AI responses are very accurate.",
  "satisfaction_score": 5,
  "meta": null
}
```

**Frontend Implementation:**
1. Add "Feedback" button in navigation bar
2. Show modal/page with form:
   - Category dropdown with icons for each type
   - Title input field
   - Description textarea
   - Conditional satisfaction_score field (only show for platform_satisfaction category)
3. After submission, show success message and feedback ID
4. **Important**: Tenant users can submit feedback but cannot view it afterward. Only platform admins can view feedback (see section 11.4).
5. Show message: "Thank you for your feedback! The Luran team will review it and reach out if needed."

---

### 11.4 List Platform FeedbackI 

**Page:** `/platform-admin/feedback`

**Endpoint:** `GET /reviews/platform`

**Authentication:** Required (Platform admin only)

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Access Control:**
- **ONLY platform admins** can view feedback
- Returns ALL feedback across ALL tenants (system-wide)
- **Tenant users CANNOT view** feedback (their own or others)
- **Note:** Tenant users can still SUBMIT feedback via POST /reviews/platform (see 11.3), but cannot view it

**Use Case:** Platform admins can view all feedback submitted by companies to monitor requests, track bugs, and manage platform improvements across all tenants.

**Query Parameters:**
- `category` (enum, optional): Filter by feedback category
  - Values: `feature_request`, `bug_report`, `performance`, `platform_satisfaction`, `documentation`, `pricing`, `general`
- `status` (string, optional): Filter by status
  - Values: `open`, `acknowledged`, `in_progress`, `resolved`, `closed`
- `start_date` (ISO 8601, optional): Filter from date
- `end_date` (ISO 8601, optional): Filter to date
- `limit` (integer, optional): Max results per page (1-100, default: 50)
- `offset` (integer, optional): Results offset for pagination (default: 0)

**Example Request:**
```
GET /reviews/platform?category=bug_report&status=open&limit=20
```

**Response (200 OK):**
```json
{
  "feedback": [
    {
      "id": "9e1e8891-9647-42fg-b45d-4e19g3f02cf9",
      "tenant_id": "1fa85f64-5717-4562-b3fc-2c963f66afa6",
      "category": "bug_report",
      "satisfaction_score": null,
      "title": "Widget not loading on Safari",
      "description": "The chat widget script fails to load on Safari 17. Console shows CORS error.",
      "status": "in_progress",
      "submitted_by": "2ga85f64-5717-4562-b3fc-2c963f66afa7",
      "meta": {
        "browser": "Safari 17",
        "os": "macOS"
      },
      "created_at": "2026-01-22T10:00:00Z",
      "updated_at": "2026-01-23T09:00:00Z"
    },
    {
      "id": "af2f9902-a758-53gh-c56e-5f20h4g13dg0",
      "tenant_id": "2fb85f64-5717-4562-b3fc-2c963f66afa7",
      "category": "feature_request",
      "satisfaction_score": null,
      "title": "Export chat transcripts",
      "description": "Need ability to export chat transcripts as PDF or CSV for compliance purposes.",
      "status": "acknowledged",
      "submitted_by": "3gc85f64-5717-4562-b3fc-2c963f66afa8",
      "meta": null,
      "created_at": "2026-01-20T14:30:00Z",
      "updated_at": "2026-01-21T08:00:00Z"
    }
  ],
  "total": 347,
  "page": 1,
  "page_size": 50
}
```

**Note:** Platform admins see feedback from ALL tenants. Notice the `tenant_id` field differs across feedback items - this is a system-wide view.

**Frontend Best Practices (Platform Admin Dashboard):**
1. **Display tenant information**: Show company name alongside each feedback item (fetch from tenant_id)
2. Group feedback by status (tabs or filters)
3. Show status badge with color coding:
   - `open`: Blue
   - `acknowledged`: Yellow
   - `in_progress`: Orange
   - `resolved`: Green
   - `closed`: Gray
4. Allow filtering by:
   - Category and status
   - Specific tenant (dropdown with all company names)
   - Date range
5. Show most recent feedback first
6. Display update timestamp to track resolution progress
7. Link to view full feedback details and update status
8. Add "Assign to team member" functionality
9. Show feedback statistics dashboard (total open, average resolution time, etc.)

**Platform Admin Feedback Dashboard UI Example:**
```
┌──────────────────────────────────────────────────────────────┐
│  Platform Feedback (All Tenants)                             │
├──────────────────────────────────────────────────────────────┤
│  [All] [Feature Requests] [Bug Reports] [Satisfaction]       │
│  Filter by tenant: [All Companies ▼]                         │
│                                                               │
│  ┌───────────────────────────────────────────────────┐      │
│  │ 🐛 Widget not loading on Safari                   │      │
│  │ Company: Acme Corp                                │      │
│  │ Status: In Progress  Updated: 1 day ago           │      │
│  │ The chat widget script fails to load on...        │      │
│  │ [View Details] [Update Status] [Assign]           │      │
│  └───────────────────────────────────────────────────┘      │
│                                                               │
│  ┌───────────────────────────────────────────────────┐      │
│  │ 💡 Export chat transcripts                        │      │
│  │ Company: TechStart Inc                            │      │
│  │ Status: Acknowledged  Updated: 2 days ago         │      │
│  │ Need ability to export chat transcripts...        │      │
│  │ [View Details] [Update Status] [Assign]           │      │
│  └────────────────────────────────────────────────┘    │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

**Status Definitions:**
- `open`: Feedback submitted, waiting for review
- `acknowledged`: Luran team has seen the feedback
- `in_progress`: Work has started on this feedback
- `resolved`: Issue fixed or feature implemented
- `closed`: Feedback completed and archived

---

### 11.5 Submit Public Feedback (Landing Page)

**Page:** Landing page feedback form or footer feedback button

**Endpoint:** `POST /reviews/public-feedback`

**Authentication:** None required (PUBLIC endpoint)

**Use Case:** Allow anyone visiting the Luran landing page to provide feedback about the platform, report issues, request features, or share their experience. This helps gather insights from potential customers and general users without requiring registration.

**Request:**
```json
{
  "category": "feature_request",
  "title": "Add Slack integration",
  "description": "Would love to see Slack integration so tickets can be created directly from Slack channels.",
  "satisfaction_score": 4,
  "name": "John Doe",
  "email": "john@example.com",
  "company": "Example Corp",
  "meta": {
    "referral_source": "google_search",
    "page_url": "https://luranai.com/features"
  }
}
```

**Request Fields:**
- `category` (enum, required): Type of feedback
  - `feature_request`: Request new features
  - `bug_report`: Report bugs or issues
  - `performance`: Performance-related feedback
  - `platform_satisfaction`: Rate overall platform satisfaction
  - `documentation`: Documentation feedback
  - `pricing`: Pricing-related feedback
  - `general`: General feedback
- `title` (string, required): Feedback title/summary (1-500 chars)
- `description` (string, required): Detailed feedback description (1-10000 chars)
- `satisfaction_score` (integer, optional): 1-5 rating (optional for any category)
- `name` (string, optional): Submitter name for follow-up (max 255 chars)
- `email` (string, optional): Contact email for follow-up (max 255 chars)
- `company` (string, optional): Company/organization name (max 255 chars)
- `meta` (object, optional): Additional context (e.g., referral source, page URL, user agent)

**Response (201 Created):**
```json
{
  "id": "be2f0013-b869-54hi-d67f-6g21i5h24eh1",
  "category": "feature_request",
  "satisfaction_score": 4,
  "title": "Add Slack integration",
  "description": "Would love to see Slack integration so tickets can be created directly from Slack channels.",
  "name": "John Doe",
  "email": "john@example.com",
  "company": "Example Corp",
  "status": "open",
  "meta": {
    "referral_source": "google_search",
    "page_url": "https://luranai.com/features"
  },
  "created_at": "2026-01-25T01:30:00Z",
  "updated_at": "2026-01-25T01:30:00Z"
}
```

**Error Responses:**

```json
// Missing required field (422)
{
  "detail": [
    {
      "loc": ["body", "title"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}

// Invalid category (422)
{
  "detail": [
    {
      "loc": ["body", "category"],
      "msg": "value is not a valid enumeration member; permitted: 'feature_request', 'bug_report', 'performance', 'platform_satisfaction', 'documentation', 'pricing', 'general'",
      "type": "type_error.enum"
    }
  ]
}

// Invalid satisfaction_score (422)
{
  "detail": [
    {
      "loc": ["body", "satisfaction_score"],
      "msg": "ensure this value is less than or equal to 5",
      "type": "value_error.number.not_le"
    }
  ]
}
```

**Frontend Implementation (Landing Page):**

1. **Feedback Form Location:**
   - Add "Give Feedback" button in landing page footer
   - Optional: Floating feedback button on the side
   - Modal or dedicated `/feedback` page

2. **Form Fields:**
   ```html
   <!-- Category Selection -->
   <select name="category">
     <option value="feature_request">💡 Feature Request</option>
     <option value="bug_report">🐛 Bug Report</option>
     <option value="performance">⚡ Performance Issue</option>
     <option value="platform_satisfaction">⭐ Rate Luran</option>
     <option value="documentation">📚 Documentation</option>
     <option value="pricing">💰 Pricing Feedback</option>
     <option value="general">💬 General Feedback</option>
   </select>

   <!-- Required Fields -->
   <input type="text" name="title" placeholder="Brief summary" required maxlength="500" />
   <textarea name="description" placeholder="Tell us more..." required maxlength="10000"></textarea>

   <!-- Optional Contact Info -->
   <input type="text" name="name" placeholder="Your name (optional)" maxlength="255" />
   <input type="email" name="email" placeholder="Email for follow-up (optional)" maxlength="255" />
   <input type="text" name="company" placeholder="Company (optional)" maxlength="255" />

   <!-- Optional Rating (1-5 stars) -->
   <div class="rating">
     <label>How would you rate Luran? (optional)</label>
     <input type="radio" name="satisfaction_score" value="5" /> ⭐⭐⭐⭐⭐
     <input type="radio" name="satisfaction_score" value="4" /> ⭐⭐⭐⭐
     <input type="radio" name="satisfaction_score" value="3" /> ⭐⭐⭐
     <input type="radio" name="satisfaction_score" value="2" /> ⭐⭐
     <input type="radio" name="satisfaction_score" value="1" /> ⭐
   </div>
   ```

3. **Metadata Tracking:**
   ```javascript
   const meta = {
     referral_source: document.referrer,
     page_url: window.location.href,
     user_agent: navigator.userAgent,
     viewport_size: `${window.innerWidth}x${window.innerHeight}`
   };
   ```

4. **After Submission:**
   - Clear form
   - Show success message: "Thank you for your feedback! We'll review it and reach out if we need more information."
   - If email provided: "We'll follow up at {email} if needed."
   - Track submission with analytics (e.g., Google Analytics event)

5. **Form Validation:**
   - Title: 1-500 characters
   - Description: 1-10,000 characters
   - Email: Valid email format (if provided)
   - Satisfaction score: 1-5 (if provided)

**Example JavaScript Implementation:**
```javascript
async function submitPublicFeedback(formData) {
  const response = await fetch('/api/v1/reviews/public-feedback', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      category: formData.category,
      title: formData.title,
      description: formData.description,
      satisfaction_score: formData.rating || null,
      name: formData.name || null,
      email: formData.email || null,
      company: formData.company || null,
      meta: {
        referral_source: document.referrer,
        page_url: window.location.href
      }
    })
  });

  if (response.ok) {
    const result = await response.json();
    showSuccessMessage('Thank you for your feedback!');
    clearForm();
    // Track with analytics
    gtag('event', 'feedback_submitted', {
      category: formData.category,
      feedback_id: result.id
    });
  } else {
    const error = await response.json();
    showErrorMessage(error.detail);
  }
}
```

**Privacy Notes:**
- Email is optional - users can submit anonymously
- Mention privacy policy: "We'll only use your email to follow up on this feedback"
- Add GDPR/privacy compliance notice if applicable

---

### 11.6 List Public Feedback (Platform Admins Only)

**Page:** `/platform-admin/public-feedback`

**Endpoint:** `GET /reviews/public-feedback`

**Authentication:** Required (Platform admin only)

**Headers:**
```
Authorization: Bearer {platform_admin_token}
```

**Access Control:**
- **ONLY platform admins** can view public feedback
- Returns ALL public feedback from landing page visitors
- **Tenant users CANNOT view** public feedback

**Use Case:** Platform admins can view all feedback submitted by landing page visitors to understand user needs, gather feature requests from potential customers, and identify platform issues before they become blockers for sign-ups.

**Query Parameters:**
- `category` (enum, optional): Filter by feedback category
  - Values: `feature_request`, `bug_report`, `performance`, `platform_satisfaction`, `documentation`, `pricing`, `general`
- `status` (string, optional): Filter by status
  - Values: `open`, `acknowledged`, `in_progress`, `resolved`, `closed`
- `start_date` (ISO 8601, optional): Filter from date
- `end_date` (ISO 8601, optional): Filter to date
- `limit` (integer, optional): Max results per page (1-100, default: 50)
- `offset` (integer, optional): Results offset for pagination (default: 0)

**Example Request:**
```
GET /reviews/public-feedback?category=feature_request&status=open&limit=20
Authorization: Bearer {platform_admin_token}
```

**Response (200 OK):**
```json
{
  "feedback": [
    {
      "id": "be2f0013-b869-54hi-d67f-6g21i5h24eh1",
      "category": "feature_request",
      "satisfaction_score": 4,
      "title": "Add Slack integration",
      "description": "Would love to see Slack integration so tickets can be created directly from Slack channels.",
      "name": "John Doe",
      "email": "john@example.com",
      "company": "Example Corp",
      "status": "acknowledged",
      "meta": {
        "referral_source": "google_search",
        "page_url": "https://luranai.com/features"
      },
      "created_at": "2026-01-25T01:30:00Z",
      "updated_at": "2026-01-25T08:00:00Z"
    },
    {
      "id": "cf3g1124-c970-65ij-e78g-7h32j6i35fi2",
      "category": "pricing",
      "satisfaction_score": null,
      "title": "Pricing page unclear",
      "description": "The pricing tiers are confusing. Not sure which plan is right for a team of 5.",
      "name": null,
      "email": null,
      "company": null,
      "status": "open",
      "meta": {
        "referral_source": "twitter",
        "page_url": "https://luranai.com/pricing"
      },
      "created_at": "2026-01-24T18:45:00Z",
      "updated_at": "2026-01-24T18:45:00Z"
    },
    {
      "id": "dg4h2235-d081-76jk-f89h-8i43k7j46gj3",
      "category": "platform_satisfaction",
      "satisfaction_score": 5,
      "title": "Impressed by the demo",
      "description": "Just watched the demo video and I'm very impressed. Will definitely try it out for my company.",
      "name": "Sarah Johnson",
      "email": "sarah.j@techcorp.io",
      "company": "TechCorp",
      "status": "acknowledged",
      "meta": {
        "referral_source": "youtube",
        "page_url": "https://luranai.com/"
      },
      "created_at": "2026-01-24T12:20:00Z",
      "updated_at": "2026-01-24T15:30:00Z"
    }
  ],
  "total": 89,
  "page": 1,
  "page_size": 50
}
```

**Response Fields:**
- `feedback` (array): List of public feedback objects
- `total` (integer): Total number of feedback items matching filters
- `page` (integer): Current page number
- `page_size` (integer): Number of results per page

**Frontend Best Practices (Platform Admin Dashboard):**

1. **Categorize Feedback:**
   - Group by category with color-coded tags
   - Feature requests (💡 Blue)
   - Bug reports (🐛 Red)
   - Pricing (💰 Green)
   - Platform satisfaction (⭐ Gold)

2. **Identify High-Value Leads:**
   - Highlight feedback with email + company name (potential customers)
   - Show "Contact info provided" badge
   - Add "Follow up" action button for items with contact details
   - Track conversion: feedback → trial signup → paid customer

3. **Anonymous vs. Identified Feedback:**
   - Show different icons for anonymous vs. named feedback
   - Filter by "Has contact info" vs. "Anonymous"
   - Prioritize named feedback with company info for sales follow-up

4. **Status Management:**
   - Dropdown to update status (open → acknowledged → in_progress → resolved → closed)
   - Show status badge with colors:
     - `open`: Blue
     - `acknowledged`: Yellow
     - `in_progress`: Orange
     - `resolved`: Green
     - `closed`: Gray

5. **Metadata Display:**
   - Show referral source (where they came from)
   - Display page URL (which page they submitted from)
   - Track patterns: "Most feedback from pricing page" → improve pricing clarity

6. **Filtering Options:**
   - Category filter (dropdown)
   - Status filter (tabs or dropdown)
   - Date range picker
   - Search by keyword in title/description
   - Filter by "Has email" / "Anonymous"

7. **Analytics Dashboard:**
   - Total feedback count by category
   - Feedback trends over time
   - Conversion rate: feedback → signup
   - Most requested features
   - Common pain points (bug reports, pricing confusion)

8. **Follow-Up Actions:**
   - "Email submitter" button (if email provided)
   - "Mark as sales lead" for potential customers
   - "Create feature request ticket" in project management
   - "Add to newsletter" for interested visitors

**Platform Admin Public Feedback Dashboard UI Example:**
```
┌────────────────────────────────────────────────────────────────┐
│  Public Landing Page Feedback                                  │
├────────────────────────────────────────────────────────────────┤
│  [All] [Feature Requests] [Bug Reports] [Pricing] [Reviews]    │
│  Filter: [Has Contact Info ✓] Status: [Open ▼]                 │
│                                                                 │
│  ┌─────────────────────────────────────────────────────┐      │
│  │ 💡 Add Slack integration                     👤 LEAD│      │
│  │ John Doe (john@example.com) • Example Corp         │      │
│  │ Status: Acknowledged  Updated: 5 hours ago         │      │
│  │ From: google_search → /features                    │      │
│  │ Rating: ⭐⭐⭐⭐                                      │      │
│  │ [View Details] [Email] [Update Status] [Mark Lead] │      │
│  └─────────────────────────────────────────────────────┘      │
│                                                                 │
│  ┌─────────────────────────────────────────────────────┐      │
│  │ 💰 Pricing page unclear                  🔒 ANON   │      │
│  │ Anonymous feedback                                  │      │
│  │ Status: Open  Submitted: 1 day ago                  │      │
│  │ From: twitter → /pricing                            │      │
│  │ The pricing tiers are confusing...                  │      │
│  │ [View Details] [Update Status] [Fix Pricing Page]  │      │
│  └─────────────────────────────────────────────────────┘      │
│                                                                 │
│  ┌─────────────────────────────────────────────────────┐      │
│  │ ⭐ Impressed by the demo                    👤 LEAD │      │
│  │ Sarah Johnson (sarah.j@techcorp.io) • TechCorp     │      │
│  │ Status: Acknowledged  Updated: 6 hours ago         │      │
│  │ From: youtube → /                                   │      │
│  │ Rating: ⭐⭐⭐⭐⭐                                    │      │
│  │ [View Details] [Email] [Create Trial Account]     │      │
│  └─────────────────────────────────────────────────────┘      │
│                                                                 │
└────────────────────────────────────────────────────────────────┘

📊 Quick Stats (This Month)
├─ Total Feedback: 89
├─ Feature Requests: 34
├─ With Contact Info: 42 (47%)
├─ Avg Satisfaction: ⭐⭐⭐⭐ 4.3
└─ Conversion to Trial: 12 (29% of leads)
```

**Key Differences from Platform Feedback (11.4):**

| Feature | Platform Feedback (11.3-11.4) | Public Feedback (11.5-11.6) |
|---------|-------------------------------|------------------------------|
| **Submitters** | Registered tenant users | Landing page visitors (anyone) |
| **Authentication** | Required (tenant login) | None (public endpoint) |
| **Tenant Association** | Has tenant_id, submitted_by | No tenant_id (pre-signup) |
| **Contact Info** | Automatic from user profile | Optional (name, email, company) |
| **Use Case** | Existing customers improving platform | Potential customers, general feedback |
| **Follow-Up** | Internal platform improvement | Sales leads, conversion opportunities |
| **View Access** | Platform admins only | Platform admins only |

**Workflow Recommendation:**
1. **Daily Review**: Check new public feedback for sales leads
2. **Follow Up**: Email visitors who left contact info within 24 hours
3. **Product Insights**: Track most requested features from potential customers
4. **Marketing**: Use positive feedback (5-star) as testimonials (with permission)
5. **Conversion**: Offer free trials to engaged visitors who provided contact info

---

## 12. Subscription & Payment System

Luran implements a **tier-based subscription system** with automatic trial period, recurring Paystack payments, and usage limit enforcement.

---

### Overview

**Key Features:**
- ✅ **30-Day Free Trial** - Automatically starts on company registration
- ✅ **4 Subscription Tiers** - Trial, Lite, Professional, Enterprise
- ✅ **Recurring Billing** - Paystack subscription plans (monthly & annual)
- ✅ **Usage Limits** - Conversations, knowledge sources, team members, storage
- ✅ **Trial Expiration Enforcement** - Platform blocks access after 30 days
- ✅ **Webhook Integration** - Automatic subscription updates from Paystack
- ✅ **Payment Transaction Records** - Full audit trail of all payments

**Subscription Flow:**
```
Registration → 30-Day Trial → Trial Expires → Payment Required → Active Subscription
```

**Payment Integration:**
- Payment processor: Paystack (Nigeria)
- Subscription model: Recurring (automatic monthly/annual billing)
- Webhook events: charge.success, subscription.create, invoice.payment_failed, etc.
- Supported channels: Card, Bank Transfer, USSD

---

### Pricing Tiers

| Feature | Trial (30 days) | Lite | Professional | Enterprise |
|---------|-----------------|------|--------------|------------|
| **Monthly Price** | ₦0 | ₦12,000 | ₦35,000 | Custom |
| **Annual Price** | N/A | ₦120,000 (save ₦24k) | ₦350,000 (save ₦70k) | Custom |
| | | | | |
| **Conversations/month** | 100 | 1,000 | 5,000 | Unlimited |
| **Knowledge sources** | 2 (files only) | 10 (files+URLs) | 30 (files+URLs) | Unlimited |
| **Storage** | 10 MB | 50 MB | 200 MB | Unlimited |
| **URL scraping** | ❌ | ✅ | ✅ | ✅ |
| **Team members** | 1 | 3 | 10 | Unlimited |
| **AI tokens/response** | 800 | 1,000 | 1,500 | 2,000 |
| | | | | |
| **Analytics** | ❌ | ✅ Basic | ✅ Advanced | ✅ Custom |
| **API access** | ❌ | ❌ | ✅ | ✅ |
| **Support** | Community | Email 24-48h | Priority 4-8h | Dedicated 1-2h |

**Annual Billing Discount:** 2 months free (10 months price for 12 months service)

---

### Tenant Subscription Fields

Each tenant has subscription tracking fields automatically managed by the system:

```json
{
  "subscription_tier": "trial",              // trial, lite, professional, enterprise
  "subscription_status": "trialing",         // trialing, active, past_due, canceled, trial_expired
  "billing_cycle": null,                     // monthly, annual (null for trial)
  "trial_ends_at": "2026-02-17T10:00:00Z",  // When 30-day trial expires
  "subscription_starts_at": null,            // When paid subscription started
  "subscription_ends_at": null               // When subscription ends (if canceled)
}
```

**Subscription Status Values:**
- `trialing` - Active 30-day trial period
- `active` - Paid subscription is active
- `past_due` - Payment failed, grace period
- `canceled` - Subscription was canceled
- `trial_expired` - Trial period ended, upgrade required

---

### 12.1 Initialize Subscription Payment

Start the payment process by creating a Paystack checkout session for recurring subscription.

**Page:** `/dashboard/upgrade` or `/dashboard/billing`

**Endpoint:** `POST /subscription/upgrade/initialize`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```json
{
  "tier": "lite",
  "billing_cycle": "annual"
}
```

**Tier Options:**
- `lite` - Lite plan
- `professional` - Professional plan
- `enterprise` - Contact sales (not available via API)

**Billing Cycle Options:**
- `monthly` - Monthly billing
- `annual` - Annual billing (2 months free)

**Response:**
```json
{
  "authorization_url": "https://checkout.paystack.com/abc123xyz",
  "access_code": "abc123xyz",
  "reference": "luran_sub_71ca7137_eb06291f",
  "amount": 12000000,
  "currency": "NGN",
  "plan_code": "PLN_jyimgdb5eobje4a"
}
```

**Response Fields:**
- `authorization_url` - Redirect user to this URL to complete payment
- `access_code` - Paystack access code (for alternative payment methods)
- `reference` - Unique transaction reference (format: `luran_sub_{tenant_id_prefix}_{random}`)
- `amount` - Amount in kobo (₦1 = 100 kobo). Example: 12000000 kobo = ₦120,000
- `currency` - Always "NGN" (Nigerian Naira)
- `plan_code` - Paystack plan code for recurring subscription

**Frontend Implementation:**

```javascript
// Step 1: Initialize payment
async function initiateUpgrade(tier, billingCycle) {
  try {
    const response = await fetch('/api/v1/subscription/upgrade/initialize', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${tenantToken}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        tier: tier,           // 'lite' or 'professional'
        billing_cycle: billingCycle  // 'monthly' or 'annual'
      })
    });

    const data = await response.json();

    if (response.ok) {
      // Step 2: Redirect to Paystack checkout
      window.location.href = data.authorization_url;

      // Alternative: Open in popup
      // const popup = window.open(data.authorization_url, 'Paystack', 'width=500,height=600');
    } else {
      // Handle error
      console.error('Payment initialization failed:', data.detail);
      showError(data.detail);
    }
  } catch (error) {
    console.error('Network error:', error);
    showError('Failed to initiate payment. Please try again.');
  }
}

// Example: User clicks "Upgrade to Lite (Annual)" button
document.getElementById('upgrade-lite-annual-btn').addEventListener('click', () => {
  initiateUpgrade('lite', 'annual');
});
```

**What Happens:**
1. System validates tier and billing cycle
2. Retrieves pricing from `PaystackService.PRICING` (₦12,000/month or ₦120,000/year for Lite)
3. Gets Paystack plan code for recurring subscription (e.g., `PLN_jyimgdb5eobje4a` for Lite monthly)
4. Generates unique reference: `luran_sub_{tenant_id[:8]}_{uuid[:8]}`
5. Creates Paystack transaction via `/transaction/initialize` with plan parameter
6. Creates `PaymentTransaction` record with status "pending"
7. Returns Paystack checkout URL and transaction details

**Paystack Checkout Flow:**
1. User is redirected to `authorization_url`
2. User completes payment on Paystack (card, bank transfer, USSD, etc.)
3. Paystack redirects back to your callback URL with reference
4. Your frontend calls verification endpoint to confirm payment

**Test Cards (Paystack Sandbox):**
- Success: `4084084084084081`
- Insufficient funds: `5061020000000000464`
- Expired card: `4084080000000409`

**Error Responses:**

```json
// 400 Bad Request - Invalid tier
{
  "detail": "Invalid tier: premium"
}

// 400 Bad Request - Invalid billing cycle
{
  "detail": "Invalid billing cycle: quarterly"
}

// 400 Bad Request - Enterprise not available via API
{
  "detail": "Enterprise pricing is custom. Please contact sales."
}

// 403 Forbidden - Already on paid plan
{
  "detail": "Cannot downgrade subscription via this endpoint"
}

// 502 Bad Gateway - Paystack API error
{
  "detail": "Payment gateway error: Invalid API key"
}
```

---

### 12.2 Verify Subscription Payment

Verify payment status after user returns from Paystack checkout.

**Page:** `/dashboard/upgrade/callback` or `/dashboard/billing`

**Endpoint:** `POST /subscription/upgrade/verify`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```json
{
  "reference": "luran_sub_71ca7137_eb06291f"
}
```

**Response (Success):**
```json
{
  "status": "success",
  "reference": "luran_sub_71ca7137_eb06291f",
  "amount": 12000000,
  "currency": "NGN",
  "paid_at": "2026-01-27T14:30:00Z",
  "channel": "card",
  "customer_email": "admin@testcompany.com",
  "subscription_code": "SUB_abc123xyz",
  "metadata": {
    "tenant_id": "71ca7137-8f94-4f11-8e81-2ac2d4126ed4",
    "tier": "lite",
    "billing_cycle": "annual",
    "subscription_type": "recurring"
  }
}
```

**Response Fields:**
- `status` - Payment status: `success`, `failed`, `pending`
- `reference` - Transaction reference
- `amount` - Amount paid in kobo
- `currency` - Currency code (NGN)
- `paid_at` - When payment was completed (ISO 8601)
- `channel` - Payment channel: card, bank, ussd, qr, mobile_money
- `customer_email` - Customer's email from Paystack
- `subscription_code` - Paystack subscription code for recurring billing (important!)
- `metadata` - Original transaction metadata

**Response (Failed):**
```json
{
  "status": "failed",
  "reference": "luran_sub_71ca7137_eb06291f",
  "amount": 12000000,
  "currency": "NGN",
  "metadata": {
    "tenant_id": "71ca7137-8f94-4f11-8e81-2ac2d4126ed4",
    "tier": "lite",
    "billing_cycle": "annual"
  }
}
```

**Frontend Implementation:**

```javascript
// Step 3: Verify payment after Paystack redirect
async function verifyPayment(reference) {
  try {
    const response = await fetch('/api/v1/subscription/upgrade/verify', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${tenantToken}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        reference: reference
      })
    });

    const data = await response.json();

    if (response.ok) {
      if (data.status === 'success') {
        // Payment successful
        showSuccess(`Payment successful! Your subscription is now active.`);
        showSubscriptionDetails(data);

        // Redirect to dashboard after 3 seconds
        setTimeout(() => {
          window.location.href = '/dashboard';
        }, 3000);
      } else if (data.status === 'failed') {
        // Payment failed
        showError('Payment failed. Please try again.');
      } else {
        // Payment pending
        showWarning('Payment is being processed. We will notify you when complete.');
      }
    } else {
      console.error('Verification failed:', data.detail);
      showError(data.detail);
    }
  } catch (error) {
    console.error('Network error:', error);
    showError('Failed to verify payment. Please contact support.');
  }
}

// Extract reference from URL query parameter
const urlParams = new URLSearchParams(window.location.search);
const reference = urlParams.get('reference');

if (reference) {
  verifyPayment(reference);
}
```

**Callback URL Setup:**

Your callback URL should be set in the initialization request:
```javascript
// Set custom callback URL (optional)
const response = await fetch('/api/v1/subscription/upgrade/initialize', {
  method: 'POST',
  headers: { /* ... */ },
  body: JSON.stringify({
    tier: 'lite',
    billing_cycle: 'annual',
    callback_url: 'https://yourdomain.com/dashboard/upgrade/callback'  // Optional
  })
});
```

Default callback: `{FRONTEND_URL}/dashboard/settings/subscription/callback`

**What Happens:**
1. System queries Paystack API: `GET /transaction/verify/{reference}`
2. Validates transaction status and amount
3. Returns transaction details including subscription code
4. **Note:** Webhook handles the actual subscription upgrade (not this endpoint)

**Why Separate Verification?**
- **Verification** (this endpoint) - Check payment status for UI feedback
- **Webhook** (backend only) - Actually upgrades the tenant subscription

This separation ensures:
- UI can show immediate feedback
- Backend processes upgrades reliably via webhook
- No race conditions or duplicate upgrades

**Error Responses:**

```json
// 404 Not Found - Transaction doesn't exist
{
  "detail": "Transaction verification failed: Transaction not found"
}

// 400 Bad Request - Invalid reference format
{
  "detail": "Invalid reference format"
}

// 502 Bad Gateway - Paystack API error
{
  "detail": "Payment verification error: Paystack API unavailable"
}
```

---

### 12.3 Direct Subscription Upgrade (Deprecated)

**⚠️ DEPRECATED:** This endpoint is for testing/admin purposes only. Production upgrades should use the payment flow (12.1 + 12.2).

**Endpoint:** `POST /subscription/upgrade`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```json
{
  "tier": "professional",
  "billing_cycle": "monthly"
}
```

**Response:**
```json
{
  "message": "Subscription upgraded successfully",
  "tier": "professional",
  "status": "active",
  "billing_cycle": "monthly"
}
```

**What Happens:**
1. Bypasses payment processing
2. Directly upgrades tenant to specified tier
3. Updates `subscription_tier`, `subscription_status`, `billing_cycle`
4. Updates tenant capabilities based on tier configuration
5. Clears trial period fields

**Use Cases:**
- Testing subscription features without payment
- Admin manually upgrading accounts
- Handling enterprise contracts

**Production Warning:**
- Does NOT create payment records
- Does NOT charge customer
- Does NOT set up recurring billing
- Should be disabled in production or restricted to platform admins

---

### 12.4 View Current Subscription

Get details about the current user's subscription including status, billing cycle, capabilities, and trial/expiry dates.

**Page:** `/dashboard/subscription` or `/dashboard/billing`

**Endpoint:** `GET /subscription/my-subscription`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** Any authenticated user (not just TENANT_ADMIN)

**Response:**
```json
{
  "tenant_id": "123e4567-e89b-12d3-a456-426614174000",
  "tenant_name": "Acme Corp",
  "subscription_tier": "professional",
  "subscription_status": "active",
  "billing_cycle": "annual",
  "trial_ends_at": null,
  "subscription_starts_at": "2026-01-15T10:00:00Z",
  "subscription_ends_at": null,
  "next_billing_date": "2027-01-15T10:00:00Z",
  "can_upgrade": true,
  "can_cancel": true,
  "is_trial": false,
  "days_until_expiry": null
}
```

**Response Fields:**
- `subscription_tier` - Current tier: `trial`, `lite`, `professional`, `enterprise`
- `subscription_status` - Status: `trialing`, `active`, `past_due`, `canceled`, `trial_expired`
- `billing_cycle` - Billing cycle: `monthly`, `annual` (null for trial)
- `trial_ends_at` - When trial expires (null if not on trial)
- `subscription_starts_at` - When paid subscription started (null if on trial)
- `subscription_ends_at` - When subscription ends if canceled (null if active)
- `next_billing_date` - Next automatic payment date (null if canceled or trial)
- `can_upgrade` - Whether user can upgrade (trial or lower tier)
- `can_cancel` - Whether user can cancel (active paid subscription)
- `is_trial` - Whether currently on trial period
- `days_until_expiry` - Days remaining until trial/subscription expires (null if no expiry)

**Frontend Example:**
```typescript
async function loadSubscriptionInfo() {
  try {
    const response = await fetch('/api/v1/subscription/my-subscription', {
      headers: {
        'Authorization': `Bearer ${tenantToken}`
      }
    });

    const subscription = await response.json();

    // Display subscription tier badge
    document.getElementById('tier-badge').textContent = subscription.subscription_tier.toUpperCase();

    // Show trial expiry warning if applicable
    if (subscription.is_trial && subscription.days_until_expiry <= 7) {
      showTrialExpiryWarning(subscription.days_until_expiry);
    }

    // Enable/disable upgrade button
    document.getElementById('upgrade-btn').disabled = !subscription.can_upgrade;

    // Enable/disable cancel button
    document.getElementById('cancel-btn').disabled = !subscription.can_cancel;

    // Display next billing date
    if (subscription.next_billing_date) {
      document.getElementById('next-billing').textContent =
        `Next billing: ${new Date(subscription.next_billing_date).toLocaleDateString()}`;
    }
  } catch (error) {
    console.error('Failed to load subscription:', error);
  }
}
```

---

### 12.5 Cancel Subscription

Cancel the current user's active subscription. Only TENANT_ADMIN users can cancel.

**Page:** `/dashboard/subscription/cancel`

**Endpoint:** `POST /subscription/cancel`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** TENANT_ADMIN

**Request:**
```json
{
  "reason": "Found a better alternative",
  "immediate": false
}
```

**Request Fields:**
- `reason` - Optional cancellation reason (for feedback)
- `immediate` - If `true`, cancel immediately (may be eligible for prorated refund). If `false`, cancel at end of billing period (default)

**Response:**
```json
{
  "message": "Subscription will be canceled on 2027-01-15",
  "status": "active",
  "ends_at": "2027-01-15T10:00:00Z",
  "refund_eligible": false,
  "can_reactivate_until": "2027-01-15T10:00:00Z"
}
```

**Response Fields:**
- `message` - Confirmation message
- `status` - New subscription status (`active` if end-of-period, `canceled` if immediate)
- `ends_at` - When subscription will end
- `refund_eligible` - Whether user may be eligible for refund (true for immediate cancellation)
- `can_reactivate_until` - Deadline to reactivate before data deletion (30-day grace period)

**Cancellation Behavior:**
- **Immediate Cancellation** (`immediate: true`):
  - Subscription canceled right away
  - May be eligible for prorated refund (contact support)
  - 30-day grace period to reactivate
  - After 30 days, data is permanently deleted

- **End-of-Period Cancellation** (`immediate: false`):
  - Subscription remains active until end of current billing cycle
  - No refund (you paid for full period)
  - Service available until end date
  - Can reactivate until end date

**Important Notes:**
- Cannot cancel trial subscriptions (they expire automatically)
- Cannot cancel already canceled subscriptions
- Cancellation is recorded in subscription history with admin info
- After cancellation, data is retained for 30 days, then permanently deleted

**Frontend Example:**
```typescript
async function cancelSubscription(immediate = false) {
  // Show confirmation dialog
  const confirmed = confirm(
    immediate
      ? 'Cancel subscription immediately? You may be eligible for a prorated refund.'
      : 'Cancel at end of billing period? Your service will remain active until then.'
  );

  if (!confirmed) return;

  try {
    const response = await fetch('/api/v1/subscription/cancel', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${tenantToken}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        reason: document.getElementById('cancel-reason').value || null,
        immediate: immediate
      })
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail);
    }

    const result = await response.json();

    // Show success message
    alert(result.message);

    // Redirect to dashboard
    window.location.href = '/dashboard';
  } catch (error) {
    console.error('Cancellation failed:', error);
    alert(`Failed to cancel: ${error.message}`);
  }
}
```

**Error Responses:**
```json
// 400 Bad Request - Cannot cancel trial
{
  "detail": "Cannot cancel trial subscription. Trial will expire automatically."
}

// 400 Bad Request - Already canceled
{
  "detail": "Subscription is already canceled"
}

// 403 Forbidden - Not tenant admin
{
  "detail": "Only TENANT_ADMIN can cancel subscriptions"
}
```

---

### 12.6 View Subscription History

Get a chronological list of all subscription changes for the current tenant.

**Page:** `/dashboard/subscription/history` or `/dashboard/billing/history`

**Endpoint:** `GET /subscription/history`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Required Role:** Any authenticated user (not just TENANT_ADMIN)

**Query Parameters:**
- `limit` - Maximum number of records to return (default: 20, max: 100)

**Example:**
```
GET /subscription/history?limit=50
```

**Response:**
```json
[
  {
    "id": "789e4567-e89b-12d3-a456-426614174999",
    "tenant_id": "123e4567-e89b-12d3-a456-426614174000",
    "action": "upgrade",
    "previous_tier": "trial",
    "new_tier": "professional",
    "previous_status": "trialing",
    "new_status": "active",
    "changed_by_type": "user",
    "changed_by_name": "admin@acmecorp.com",
    "reason": "User upgraded via payment",
    "created_at": "2026-01-15T10:00:00Z"
  },
  {
    "id": "890e4567-e89b-12d3-a456-426614174888",
    "tenant_id": "123e4567-e89b-12d3-a456-426614174000",
    "action": "extend_trial",
    "previous_tier": "trial",
    "new_tier": "trial",
    "previous_status": "trial",
    "new_status": "trial",
    "changed_by_type": "platform_admin",
    "changed_by_name": "Platform Admin",
    "reason": "Extended trial for sales demo",
    "created_at": "2026-01-01T14:30:00Z"
  },
  {
    "id": "901e4567-e89b-12d3-a456-426614174777",
    "tenant_id": "123e4567-e89b-12d3-a456-426614174000",
    "action": "cancellation",
    "previous_tier": "lite",
    "new_tier": "lite",
    "previous_status": "active",
    "new_status": "canceled",
    "changed_by_type": "user",
    "changed_by_name": "admin@acmecorp.com",
    "reason": "User requested cancellation",
    "created_at": "2025-12-20T09:15:00Z"
  }
]
```

**Response Fields:**
- `id` - Unique history entry ID
- `tenant_id` - Tenant UUID
- `action` - Action type: `upgrade`, `downgrade`, `cancellation`, `override`, `extend_trial`, `tier_change`
- `previous_tier` - Tier before change (trial, lite, professional, enterprise)
- `new_tier` - Tier after change
- `previous_status` - Status before change
- `new_status` - Status after change
- `changed_by_type` - Who made the change: `user`, `platform_admin`, `webhook`
- `changed_by_name` - Name/email of person who made the change
- `reason` - Explanation for the change (user input or system message)
- `created_at` - When the change occurred (ISO 8601 timestamp)

**Frontend Example:**
```typescript
async function loadSubscriptionHistory() {
  try {
    const response = await fetch('/api/v1/subscription/history?limit=20', {
      headers: {
        'Authorization': `Bearer ${tenantToken}`
      }
    });

    const history = await response.json();

    // Render history timeline
    const historyContainer = document.getElementById('subscription-history');
    historyContainer.innerHTML = history.map(entry => `
      <div class="history-entry">
        <div class="entry-date">${new Date(entry.created_at).toLocaleDateString()}</div>
        <div class="entry-action">
          <strong>${entry.action.replace('_', ' ').toUpperCase()}</strong>
          ${entry.previous_tier !== entry.new_tier
            ? `<span>${entry.previous_tier} → ${entry.new_tier}</span>`
            : ''}
        </div>
        <div class="entry-details">
          ${entry.reason || 'No reason provided'}
        </div>
        <div class="entry-author">
          Changed by: ${entry.changed_by_name} (${entry.changed_by_type})
        </div>
      </div>
    `).join('');
  } catch (error) {
    console.error('Failed to load history:', error);
  }
}
```

**Common Actions:**
- `upgrade` - User upgraded to higher tier (via payment or admin)
- `downgrade` - Downgraded to lower tier
- `cancellation` - Subscription was canceled
- `override` - Platform admin manually changed subscription
- `extend_trial` - Platform admin extended trial period
- `tier_change` - Generic tier change (not upgrade/downgrade)

---

### Summary

**What You've Learned:**

1. **Subscription System** - 4 tiers with automatic trial period
2. **Payment Flow** - Initialize → Paystack checkout → Webhook → Verification
3. **Enforcement** - HTTP 402 errors for expired trials and limits
4. **Recurring Billing** - Automatic monthly/annual renewals via Paystack
5. **Webhooks** - Backend automatically processes payment events
6. **Frontend Implementation** - Complete upgrade page example

**Key Endpoints:**
- `POST /subscription/upgrade/initialize` - Start payment
- `POST /subscription/upgrade/verify` - Confirm payment
- `GET /subscription/my-subscription` - View current subscription
- `POST /subscription/cancel` - Cancel subscription
- `GET /subscription/history` - View subscription change history
- `GET /subscription/plans` - Get public pricing (no auth required)
- `POST /webhooks/paystack` - Process payment events (backend)

**Next Steps:**
1. Build upgrade page using example code
2. Test payment flow with Paystack test cards
3. Implement subscription status banners
4. Add limit usage displays (e.g., "Using 45/100 conversations")
5. Create billing history page using GET /subscription/history
6. Add "View Subscription" page using GET /subscription/my-subscription
7. Implement subscription cancellation flow

For complete details on subscription enforcement, webhooks, recurring billing, testing checklist, and troubleshooting, see the full documentation in `/docs/SUBSCRIPTION_PRICING_MODEL.md`.

---

## 13. Platform Admin Subscription Management

**New Feature:** Complete platform admin tools for subscription management, manual overrides, trial extensions, and payment history. Added 2026-01-27.

### Overview

Platform admins can now:
- **Manually upgrade/downgrade** any tenant (for enterprise deals, promotions)
- **Extend trial periods** (for sales demos, support issues)
- **View subscription history** (full audit trail with admin names)
- **Filter tenants** by subscription status, tier, or trial expiration
- **View payment history** (all transactions with filters)
- **Track capability overrides** (manual limits that persist through upgrades)

### 13.1 Get Public Pricing Plans (No Auth Required)

**Endpoint:** `GET /subscription/plans`
**Auth:** None (PUBLIC endpoint)
**Use Cases:** Marketing website, signup flow, mobile app pricing display

```typescript
// Fetch pricing plans
const response = await fetch('http://localhost:8000/api/v1/subscription/plans');
const plans = await response.json();

// Response
[
  {
    "tier": "trial",
    "display_name": "Trial",
    "description": "Try out the platform with limited features",
    "monthly_price_kobo": 0,
    "annual_price_kobo": 0,
    "monthly_price_naira": 0.0,
    "annual_price_naira": 0.0,
    "annual_savings_naira": 0.0,
    "annual_discount_percentage": 0.0,
    "capabilities": {
      "can_access_analytics": false,
      "max_knowledge_sources": 2,
      "can_use_url_sources": false,
      "max_knowledge_mb": 10,
      "max_conversations_per_month": 100,
      "max_team_members": 1,
      "max_response_tokens": 800
    },
    "features": [
      "2 knowledge sources",
      "10MB storage",
      "100 conversations/month",
      "1 team member",
      "Email support"
    ],
    "is_popular": false
  },
  {
    "tier": "lite",
    "display_name": "Lite",
    "description": "Perfect for small businesses getting started",
    "monthly_price_kobo": 1200000,  // ₦12,000
    "annual_price_kobo": 12000000,  // ₦120,000
    "monthly_price_naira": 12000.0,
    "annual_price_naira": 120000.0,
    "annual_savings_naira": 24000.0,
    "annual_discount_percentage": 16.7,
    "capabilities": { /* ... */ },
    "features": [ /* ... */ ],
    "is_popular": false
  },
  {
    "tier": "professional",
    "display_name": "Professional",
    "description": "Advanced features for growing teams",
    "monthly_price_kobo": 3500000,  // ₦35,000
    "annual_price_kobo": 35000000,  // ₦350,000
    "monthly_price_naira": 35000.0,
    "annual_price_naira": 350000.0,
    "annual_savings_naira": 70000.0,
    "annual_discount_percentage": 16.7,
    "capabilities": { /* ... */ },
    "features": [ /* ... */ ],
    "is_popular": true  // Marked as recommended
  }
]
```

**Note:** Enterprise tier is excluded from public pricing plans. It requires custom pricing and should be handled via "Contact Sales" flow.

**Frontend Example - Pricing Page:**

```typescript
async function displayPricingPlans() {
  try {
    const response = await fetch('http://localhost:8000/api/v1/subscription/plans');
    const plans = await response.json();

    const pricingContainer = document.getElementById('pricing-plans');

    plans.forEach(plan => {
      const card = `
        <div class="pricing-card ${plan.is_popular ? 'popular' : ''}">
          <h3>${plan.display_name}</h3>
          <p class="description">${plan.description}</p>

          <div class="pricing">
            <div class="monthly">
              <span class="amount">₦${plan.monthly_price_naira.toLocaleString()}</span>
              <span class="period">/month</span>
            </div>
            <div class="annual">
              <span class="amount">₦${plan.annual_price_naira.toLocaleString()}</span>
              <span class="period">/year</span>
              ${plan.annual_discount_percentage > 0 ?
                `<span class="savings">Save ${plan.annual_discount_percentage}%</span>` : ''}
            </div>
          </div>

          <ul class="features">
            ${plan.features.map(f => `<li>${f}</li>`).join('')}
          </ul>

          <button class="select-plan" data-tier="${plan.tier}">
            ${plan.tier === 'trial' ? 'Start Free Trial' : 'Select Plan'}
          </button>
        </div>
      `;
      pricingContainer.innerHTML += card;
    });
  } catch (error) {
    console.error('Failed to load pricing:', error);
  }
}
```

### 13.2 Manual Subscription Override

**Endpoint:** `POST /platform/subscriptions/{tenant_id}/override`
**Auth:** Platform Admin (Super Admin)
**Use Cases:** Enterprise deals, promotional upgrades, VIP exceptions, support resolutions

```typescript
interface OverrideRequest {
  tier: 'trial' | 'lite' | 'professional' | 'enterprise';
  billing_cycle?: 'monthly' | 'annual';  // null for trial
  reason: string;  // Required for audit trail (10-500 chars)
  apply_tier_capabilities: boolean;  // Default: true
}

// Override subscription
const response = await fetch(
  'http://localhost:8000/api/v1/platform/subscriptions/tenant-uuid/override',
  {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      tier: 'professional',
      billing_cycle: 'monthly',
      reason: 'Enterprise deal closed - promotional upgrade for 6 months',
      apply_tier_capabilities: true  // Reset capabilities to tier defaults
    })
  }
);

// Response
{
  "success": true,
  "message": "Successfully overridden subscription to professional",
  "tenant_id": "tenant-uuid",
  "previous_tier": "trial",
  "new_tier": "professional",
  "capabilities_applied": true
}
```

**Important Notes:**
- `apply_tier_capabilities: true` - Resets ALL capabilities to tier defaults (recommended)
- `apply_tier_capabilities: false` - Preserves manual capability overrides
- All overrides logged in subscription_history with admin name for audit trail
- For trial tier, billing_cycle must be null

**Admin Dashboard Example:**

```typescript
async function upgradeToEnterprise(tenantId: string) {
  const reason = prompt('Enter reason for upgrade (required for audit):');
  if (!reason || reason.length < 10) {
    alert('Please provide a detailed reason (min 10 characters)');
    return;
  }

  try {
    const response = await fetch(
      `http://localhost:8000/api/v1/platform/subscriptions/${tenantId}/override`,
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${adminToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          tier: 'enterprise',
          billing_cycle: 'annual',
          reason: reason,
          apply_tier_capabilities: true
        })
      }
    );

    const result = await response.json();

    if (result.success) {
      alert(`Tenant upgraded from ${result.previous_tier} to ${result.new_tier}`);
      refreshTenantList();
    }
  } catch (error) {
    alert('Override failed: ' + error.message);
  }
}
```

### 13.3 Extend Trial Period

**Endpoint:** `POST /platform/subscriptions/{tenant_id}/trial/extend`
**Auth:** Platform Admin (Super Admin)
**Use Cases:** Sales demos, support issues, re-engagement campaigns

```typescript
interface TrialExtensionRequest {
  days: number;  // 1-90 days
  reason: string;  // Required for audit trail (10-500 chars)
}

// Extend trial by 14 days
const response = await fetch(
  'http://localhost:8000/api/v1/platform/subscriptions/tenant-uuid/trial/extend',
  {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      days: 14,
      reason: 'Sales demo needs more time for evaluation - enterprise deal in progress'
    })
  }
);

// Response
{
  "success": true,
  "message": "Trial extended by 14 days",
  "tenant_id": "tenant-uuid",
  "previous_trial_end": "2026-01-28T10:00:00Z",
  "new_trial_end": "2026-02-11T10:00:00Z",
  "days_extended": 14
}
```

**Important Notes:**
- Only works for tenants on trial tier (HTTP 400 if paid subscription)
- Can extend active trials or reactivate expired trials
- If trial already expired, extends from current date
- Maximum extension: 90 days
- All extensions logged in subscription_history

**Error Responses:**

```typescript
// HTTP 400 - Not a trial subscription
{
  "detail": "Can only extend trial tier subscriptions"
}

// HTTP 404 - Tenant not found
{
  "detail": "Tenant not found"
}
```

### 13.4 Get Subscription Details

**Endpoint:** `GET /platform/subscriptions/{tenant_id}`
**Auth:** Platform Admin (Super Admin)
**Returns:** Complete subscription info with history and payment stats

```typescript
const response = await fetch(
  'http://localhost:8000/api/v1/platform/subscriptions/tenant-uuid',
  {
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN'
    }
  }
);

// Response
{
  "tenant_id": "tenant-uuid",
  "tenant_name": "Acme Corp",

  // Current subscription
  "subscription_tier": "professional",
  "subscription_status": "active",
  "billing_cycle": "monthly",
  "trial_ends_at": null,
  "subscription_starts_at": "2026-01-15T10:00:00Z",

  // Override tracking
  "has_overrides": true,
  "override_fields": ["max_conversations_per_month"],  // Fields with manual overrides

  // History (last 10 changes)
  "recent_changes": [
    {
      "id": "history-uuid",
      "tenant_id": "tenant-uuid",
      "action": "upgrade",
      "previous_tier": "lite",
      "new_tier": "professional",
      "previous_status": "active",
      "new_status": "active",
      "changed_by_type": "webhook",
      "changed_by_name": "Paystack Webhook",
      "reason": "Payment successful for professional monthly",
      "created_at": "2026-01-20T10:00:00Z"
    },
    {
      "id": "history-uuid-2",
      "tenant_id": "tenant-uuid",
      "action": "override",
      "previous_tier": "trial",
      "new_tier": "lite",
      "changed_by_type": "platform_admin",
      "changed_by_name": "admin@luranai.com",
      "reason": "Early adopter program - free Lite tier for 3 months",
      "created_at": "2026-01-15T10:00:00Z"
    }
  ],

  // Payment stats
  "total_payments": 3,
  "total_revenue_naira": 45000.0  // ₦45,000
}
```

**Admin Dashboard Example:**

```typescript
async function viewSubscriptionDetails(tenantId: string) {
  const response = await fetch(
    `http://localhost:8000/api/v1/platform/subscriptions/${tenantId}`,
    { headers: { 'Authorization': `Bearer ${adminToken}` } }
  );

  const details = await response.json();

  // Display subscription info
  document.getElementById('tenant-name').textContent = details.tenant_name;
  document.getElementById('current-tier').textContent = details.subscription_tier;
  document.getElementById('status').textContent = details.subscription_status;
  document.getElementById('total-revenue').textContent =
    `₦${details.total_revenue_naira.toLocaleString()}`;

  // Show override warning
  if (details.has_overrides) {
    document.getElementById('override-warning').innerHTML = `
      ⚠️ Manual overrides active on: ${details.override_fields.join(', ')}
    `;
  }

  // Display history timeline
  const timeline = details.recent_changes.map(change => `
    <div class="timeline-item">
      <span class="date">${new Date(change.created_at).toLocaleDateString()}</span>
      <strong>${change.action}</strong>: ${change.previous_tier} → ${change.new_tier}
      <div class="meta">By: ${change.changed_by_name} | ${change.reason}</div>
    </div>
  `).join('');

  document.getElementById('history-timeline').innerHTML = timeline;
}
```

### 13.5 List Payment Transactions

**Endpoint:** `GET /platform/subscriptions/payments/all`
**Auth:** Platform Admin (Super Admin)
**Use Cases:** Financial reconciliation, failed payment review, revenue reporting

**Query Parameters:**
- `tenant_id` (optional): Filter by tenant UUID
- `status` (optional): Filter by payment status (success, pending, failed)
- `limit` (default: 100, max: 1000): Maximum results
- `offset` (default: 0): Pagination offset

```typescript
// Get all successful payments
const response = await fetch(
  'http://localhost:8000/api/v1/platform/subscriptions/payments/all?status=success&limit=50',
  {
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN'
    }
  }
);

// Response
[
  {
    "id": "payment-uuid",
    "tenant_id": "tenant-uuid",
    "tenant_name": "Acme Corp",
    "reference": "luran_sub_abc12345",
    "amount": 1500000,  // Kobo (₦15,000)
    "currency": "NGN",
    "status": "success",
    "tier": "professional",
    "billing_cycle": "monthly",
    "customer_email": "billing@acmecorp.com",
    "paid_at": "2026-01-20T10:30:00Z",
    "created_at": "2026-01-20T10:25:00Z"
  },
  // ... more transactions
]
```

**Admin Dashboard Example - Payment History:**

```typescript
async function loadPaymentHistory(filters = {}) {
  const params = new URLSearchParams();
  if (filters.tenantId) params.append('tenant_id', filters.tenantId);
  if (filters.status) params.append('status', filters.status);
  params.append('limit', '100');
  params.append('offset', filters.offset || '0');

  const response = await fetch(
    `http://localhost:8000/api/v1/platform/subscriptions/payments/all?${params}`,
    { headers: { 'Authorization': `Bearer ${adminToken}` } }
  );

  const transactions = await response.json();

  const table = transactions.map(tx => `
    <tr class="${tx.status === 'failed' ? 'failed' : ''}">
      <td>${new Date(tx.paid_at || tx.created_at).toLocaleDateString()}</td>
      <td>${tx.tenant_name}</td>
      <td>${tx.tier}</td>
      <td>₦${(tx.amount / 100).toLocaleString()}</td>
      <td><span class="status-${tx.status}">${tx.status}</span></td>
      <td>${tx.reference}</td>
    </tr>
  `).join('');

  document.getElementById('payment-table').innerHTML = table;

  // Calculate revenue
  const totalRevenue = transactions
    .filter(tx => tx.status === 'success')
    .reduce((sum, tx) => sum + tx.amount, 0);

  document.getElementById('total-revenue').textContent =
    `₦${(totalRevenue / 100).toLocaleString()}`;
}
```

### 13.6 Filter Tenants by Subscription

**Endpoint:** `GET /platform/tenants` (Enhanced with subscription filters)
**Auth:** Platform Admin (Super Admin)
**Use Cases:** Trial conversion outreach, payment follow-up, enterprise customer lists

**New Query Parameters:**
- `subscription_status` (optional): trial, active, trial_expired, past_due, canceled
- `subscription_tier` (optional): trial, lite, professional, enterprise
- `trial_expiring_days` (optional): Find trials expiring within N days (0-90)
- `limit` (default: 100, max: 1000): Maximum results
- `offset` (default: 0): Pagination offset

```typescript
// Find all trial_expired tenants (for conversion outreach)
const response = await fetch(
  'http://localhost:8000/api/v1/platform/tenants?subscription_status=trial_expired&limit=50',
  {
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN'
    }
  }
);

// Find trials expiring in 7 days (for reminder emails)
const expiringSoon = await fetch(
  'http://localhost:8000/api/v1/platform/tenants?trial_expiring_days=7',
  {
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN'
    }
  }
);

// Find all enterprise customers
const enterprise = await fetch(
  'http://localhost:8000/api/v1/platform/tenants?subscription_tier=enterprise',
  {
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN'
    }
  }
);
```

**Admin Dashboard Example - Tenant Filtering:**

```typescript
async function filterTenants() {
  const status = document.getElementById('status-filter').value;
  const tier = document.getElementById('tier-filter').value;
  const expiringDays = document.getElementById('expiring-filter').value;

  const params = new URLSearchParams();
  if (status) params.append('subscription_status', status);
  if (tier) params.append('subscription_tier', tier);
  if (expiringDays) params.append('trial_expiring_days', expiringDays);
  params.append('limit', '100');

  const response = await fetch(
    `http://localhost:8000/api/v1/platform/tenants?${params}`,
    { headers: { 'Authorization': `Bearer ${adminToken}` } }
  );

  const tenants = await response.json();

  // Display filtered tenants
  const list = tenants.map(tenant => `
    <div class="tenant-card">
      <h3>${tenant.name}</h3>
      <div class="subscription-info">
        <span class="tier">${tenant.subscription_tier}</span>
        <span class="status ${tenant.subscription_status}">
          ${tenant.subscription_status}
        </span>
      </div>
      ${tenant.trial_ends_at ? `
        <div class="trial-info">
          Trial ends: ${new Date(tenant.trial_ends_at).toLocaleDateString()}
          ${getTrialDaysRemaining(tenant.trial_ends_at) <= 7 ?
            '<span class="warning">⚠️ Expiring soon</span>' : ''}
        </div>
      ` : ''}
      <div class="actions">
        <button onclick="viewDetails('${tenant.id}')">View Details</button>
        <button onclick="extendTrial('${tenant.id}')">Extend Trial</button>
        <button onclick="upgradeToEnterprise('${tenant.id}')">Upgrade</button>
      </div>
    </div>
  `).join('');

  document.getElementById('tenant-list').innerHTML = list;
}

function getTrialDaysRemaining(trialEndsAt: string): number {
  const now = new Date();
  const end = new Date(trialEndsAt);
  return Math.ceil((end.getTime() - now.getTime()) / (1000 * 60 * 60 * 24));
}
```

---

**Platform Admin Subscription Management - Key Points:**

1. **Public Pricing API** - No auth required, perfect for marketing websites
2. **Manual Overrides** - Upgrade tenants without payment for special deals
3. **Trial Extensions** - Extend or reactivate trials for sales/support
4. **Complete History** - All changes logged with admin names for compliance
5. **Payment Tracking** - View all transactions with filters and revenue stats
6. **Smart Filtering** - Find tenants by subscription criteria for targeted outreach
7. **Override Persistence** - Manual capability changes survive tier upgrades
8. **Dynamic Pricing Management** - Update pricing and capabilities without code changes

### 13.6 Platform Pricing Management (Database-Driven)

**New Feature:** Platform admins can now manage subscription pricing and capabilities directly through the database and API, eliminating the need for code deployments for pricing changes. Added 2026-01-27.

#### 13.6.1 List All Subscription Plans (Platform Admin)

**Endpoint:** `GET /platform/pricing`
**Auth:** Platform Admin (Super Admin only)
**Use Cases:** View all plan configurations, audit current pricing

```typescript
// Fetch all subscription plans (including inactive)
const response = await fetch(
  'http://localhost:8000/api/v1/platform/pricing?include_inactive=true',
  {
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN',
    }
  }
);

const data = await response.json();

// Response
{
  "plans": [
    {
      "tier": "trial",
      "display_name": "Trial",
      "description": "Try out the platform with limited features",
      "monthly_price_kobo": 0,
      "annual_price_kobo": 0,
      "monthly_price_naira": 0.0,
      "annual_price_naira": 0.0,
      "capabilities": {
        "can_access_analytics": false,
        "max_knowledge_sources": 2,
        "can_use_url_sources": false,
        "max_knowledge_mb": 10,
        "max_conversations_per_month": 100,
        "max_team_members": 1,
        "max_response_tokens": 800
      },
      "features": [
        "2 knowledge sources",
        "10MB storage",
        "100 conversations/month",
        "1 team member",
        "Email support"
      ],
      "is_active": true,
      "sort_order": 0
    },
    {
      "tier": "lite",
      "display_name": "Lite",
      "description": "Perfect for small businesses getting started",
      "monthly_price_kobo": 1200000,  // ₦12,000
      "annual_price_kobo": 12000000,  // ₦120,000
      "monthly_price_naira": 12000.0,
      "annual_price_naira": 120000.0,
      "capabilities": {
        "can_access_analytics": true,
        "max_knowledge_sources": 10,
        "can_use_url_sources": true,
        "max_knowledge_mb": 50,
        "max_conversations_per_month": 1000,
        "max_team_members": 3,
        "max_response_tokens": 1000
      },
      "features": [
        "10 knowledge sources",
        "50MB storage",
        "1,000 conversations/month",
        "3 team members",
        "URL scraping",
        "Analytics dashboard",
        "Priority email support"
      ],
      "is_active": true,
      "sort_order": 1
    },
    {
      "tier": "professional",
      "display_name": "Professional",
      "description": "Advanced features for growing teams",
      "monthly_price_kobo": 3500000,  // ₦35,000
      "annual_price_kobo": 35000000,  // ₦350,000
      "monthly_price_naira": 35000.0,
      "annual_price_naira": 350000.0,
      "capabilities": {
        "can_access_analytics": true,
        "max_knowledge_sources": 30,
        "can_use_url_sources": true,
        "max_knowledge_mb": 200,
        "max_conversations_per_month": 5000,
        "max_team_members": 10,
        "max_response_tokens": 1500
      },
      "features": [
        "30 knowledge sources",
        "200MB storage",
        "5,000 conversations/month",
        "10 team members",
        "URL scraping",
        "Analytics dashboard",
        "Advanced AI responses",
        "Priority support"
      ],
      "is_active": true,
      "sort_order": 2
    },
    {
      "tier": "enterprise",
      "display_name": "Enterprise",
      "description": "Contact us for custom pricing and unlimited features",
      "monthly_price_kobo": 0,
      "annual_price_kobo": 0,
      "monthly_price_naira": 0.0,
      "annual_price_naira": 0.0,
      "capabilities": {
        "can_access_analytics": true,
        "max_knowledge_sources": null,  // Unlimited
        "can_use_url_sources": true,
        "max_knowledge_mb": null,  // Unlimited
        "max_conversations_per_month": null,  // Unlimited
        "max_team_members": null,  // Unlimited
        "max_response_tokens": 2000
      },
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
        "Custom integrations"
      ],
      "is_active": true,
      "sort_order": 3
    }
  ],
  "total": 4
}
```

**Query Parameters:**
- `include_inactive` (optional): Include inactive plans (default: false)

#### 13.6.2 Update Subscription Plan Pricing

**Endpoint:** `PUT /platform/pricing/{tier}`
**Auth:** Platform Admin (Super Admin only)
**Use Cases:** Adjust pricing, update capabilities, modify features, deactivate plans

```typescript
interface PricingPlanUpdateRequest {
  monthly_price_kobo?: number;  // Price in kobo (₦1 = 100 kobo)
  annual_price_kobo?: number;   // Annual price in kobo
  display_name?: string;         // Plan display name
  description?: string;          // Plan description
  capabilities?: {
    can_access_analytics?: boolean;
    max_knowledge_sources?: number;
    can_use_url_sources?: boolean;
    max_knowledge_mb?: number;
    max_conversations_per_month?: number;
    max_team_members?: number;
    max_response_tokens?: number;
  };
  features?: string[];           // List of feature descriptions
  is_active?: boolean;           // Active/inactive status
  sort_order?: number;           // Display ordering
  reason: string;                // REQUIRED: Reason for change (audit trail)
}

// Update Lite plan pricing
const response = await fetch(
  'http://localhost:8000/api/v1/platform/pricing/lite',
  {
    method: 'PUT',
    headers: {
      'Authorization': 'Bearer PLATFORM_ADMIN_TOKEN',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      monthly_price_kobo: 1500000,  // ₦15,000 (increased from ₦12,000)
      annual_price_kobo: 15000000,  // ₦150,000 (increased from ₦120,000)
      capabilities: {
        max_knowledge_sources: 15,  // Increased from 10
        max_conversations_per_month: 1500,  // Increased from 1000
      },
      features: [
        "15 knowledge sources",  // Updated from 10
        "50MB storage",
        "1,500 conversations/month",  // Updated from 1,000
        "3 team members",
        "URL scraping",
        "Analytics dashboard",
        "Priority email support"
      ],
      reason: "Market adjustment based on competitor analysis and increased feature limits"
    })
  }
);

// Response: Updated plan with new pricing
{
  "tier": "lite",
  "display_name": "Lite",
  "description": "Perfect for small businesses getting started",
  "monthly_price_kobo": 1500000,
  "annual_price_kobo": 15000000,
  "monthly_price_naira": 15000.0,
  "annual_price_naira": 150000.0,
  "capabilities": {
    "can_access_analytics": true,
    "max_knowledge_sources": 15,
    "can_use_url_sources": true,
    "max_knowledge_mb": 50,
    "max_conversations_per_month": 1500,
    "max_team_members": 3,
    "max_response_tokens": 1000
  },
  "features": [
    "15 knowledge sources",
    "50MB storage",
    "1,500 conversations/month",
    "3 team members",
    "URL scraping",
    "Analytics dashboard",
    "Priority email support"
  ],
  "is_active": true,
  "sort_order": 1
}
```

**Important Notes:**
- All pricing changes require a `reason` for audit trail
- Changes are logged in audit_logs table with admin details
- Cache is invalidated immediately (updates reflect in real-time)
- Partial updates supported (only provide fields you want to change)
- Changes take effect immediately for new subscriptions
- Existing subscriptions maintain their pricing until renewal

**Frontend Example - Pricing Management UI:**

```typescript
async function updatePlanPricing(tier: string, updates: PricingPlanUpdateRequest) {
  try {
    const response = await fetch(
      `http://localhost:8000/api/v1/platform/pricing/${tier}`,
      {
        method: 'PUT',
        headers: {
          'Authorization': `Bearer ${adminToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(updates)
      }
    );

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Failed to update pricing');
    }

    const updatedPlan = await response.json();

    // Show success message
    showNotification('success', `${updatedPlan.display_name} plan updated successfully`);

    // Refresh pricing table
    await loadPricingPlans();

    return updatedPlan;
  } catch (error) {
    console.error('Error updating pricing:', error);
    showNotification('error', error.message);
    throw error;
  }
}

// Example: Build pricing management form
function renderPricingForm(plan: PricingPlan) {
  return `
    <form id="pricing-form-${plan.tier}" onsubmit="handlePricingUpdate(event, '${plan.tier}')">
      <h3>Update ${plan.display_name} Plan</h3>

      <!-- Pricing -->
      <div class="form-section">
        <h4>Pricing (in Naira)</h4>
        <label>
          Monthly Price:
          <input type="number" name="monthly_price"
                 value="${plan.monthly_price_naira}"
                 step="1000" min="0" />
        </label>
        <label>
          Annual Price:
          <input type="number" name="annual_price"
                 value="${plan.annual_price_naira}"
                 step="1000" min="0" />
        </label>
      </div>

      <!-- Capabilities -->
      <div class="form-section">
        <h4>Capabilities</h4>
        <label>
          <input type="checkbox" name="can_access_analytics"
                 ${plan.capabilities.can_access_analytics ? 'checked' : ''} />
          Analytics Access
        </label>
        <label>
          Max Knowledge Sources:
          <input type="number" name="max_knowledge_sources"
                 value="${plan.capabilities.max_knowledge_sources || ''}"
                 placeholder="Unlimited if empty" />
        </label>
        <label>
          Max Conversations/Month:
          <input type="number" name="max_conversations_per_month"
                 value="${plan.capabilities.max_conversations_per_month || ''}"
                 placeholder="Unlimited if empty" />
        </label>
        <label>
          Max Team Members:
          <input type="number" name="max_team_members"
                 value="${plan.capabilities.max_team_members || ''}"
                 placeholder="Unlimited if empty" />
        </label>
      </div>

      <!-- Reason (Required) -->
      <div class="form-section">
        <label>
          Reason for Change (Required):
          <textarea name="reason" required minlength="10" maxlength="500"
                    placeholder="Explain why you're making this change..."></textarea>
        </label>
      </div>

      <button type="submit">Update Plan</button>
    </form>
  `;
}

async function handlePricingUpdate(event: Event, tier: string) {
  event.preventDefault();
  const form = event.target as HTMLFormElement;
  const formData = new FormData(form);

  // Convert Naira to kobo for API
  const updates: PricingPlanUpdateRequest = {
    monthly_price_kobo: parseFloat(formData.get('monthly_price') as string) * 100,
    annual_price_kobo: parseFloat(formData.get('annual_price') as string) * 100,
    capabilities: {
      can_access_analytics: formData.get('can_access_analytics') === 'on',
      max_knowledge_sources: parseInt(formData.get('max_knowledge_sources') as string) || null,
      max_conversations_per_month: parseInt(formData.get('max_conversations_per_month') as string) || null,
      max_team_members: parseInt(formData.get('max_team_members') as string) || null,
    },
    reason: formData.get('reason') as string,
  };

  await updatePlanPricing(tier, updates);
}
```

#### 13.6.3 Pricing Change Audit Trail

All pricing changes are automatically logged with:
- Admin ID and email
- Previous state (pricing, capabilities, features)
- New state
- Reason for change
- Timestamp

View audit logs via: `GET /platform/audit?action_type=update_pricing_plan`

**Benefits of Database-Driven Pricing:**
1. **No Code Deployments** - Update pricing without pushing code
2. **Real-Time Updates** - Changes reflect immediately (5-minute cache)
3. **Full Audit Trail** - Track all pricing changes with admin accountability
4. **A/B Testing Ready** - Easy to test different pricing strategies
5. **Promotional Flexibility** - Quick response to market conditions
6. **Multi-Currency Ready** - Foundation for future currency support

**Next Steps:**
1. Build platform admin dashboard with subscription management UI
2. Implement trial expiration notification system
3. Create payment reconciliation reports
4. Set up automated trial expiration emails
5. Build tenant filtering for outreach campaigns

For implementation details, see the plan documentation and service code in `/app/services/platform_subscription_service.py`.

---

## 14. User Flows with Diagrams

### Flow 1: Company Registration

```
┌──────────────────┐
│ Visit /register  │
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────┐
│ Fill Registration Form       │
│ • Company Name               │
│ • Display Name               │
│ • Admin Name                 │
│ • Admin Email                │
└────────┬─────────────────────┘
         │
         ▼
┌──────────────────────────────┐
│ POST /auth/register-company  │
└────────┬─────────────────────┘
         │
         ▼
┌──────────────────────────────┐
│ Show Success Message         │
│ "Check your email!"          │
└────────┬─────────────────────┘
         │
         ▼
┌──────────────────────────────┐
│ User Checks Email            │
│ • Gets credentials           │
│ • Gets verification link     │
└────────┬─────────────────────┘
         │
         ▼
┌──────────────────────────────┐
│ Click Verification Link      │
│ → /verify-email?token=...    │
└────────┬─────────────────────┘
         │
         ▼
┌──────────────────────────────┐
│ POST /auth/verify-email      │
└────────┬─────────────────────┘
         │
         ▼
┌──────────────────────────────┐
│ Account Activated!           │
│ Redirect to /login           │
└──────────────────────────────┘
```

---

### Flow 2: Team Member Invitation

```
┌─────────────────────────┐
│ TENANT_ADMIN logged in  │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ Go to /dashboard/team   │
│ Click "Invite Member"   │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ Fill Invite Form        │
│ • Email                 │
│ • Name                  │
│ • Role (Agent/Viewer)   │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ POST /users             │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ Member Added!           │
│ Email sent with         │
│ credentials             │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ Member checks email     │
│ Gets temp password      │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ Member logs in          │
│ POST /auth/login        │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ Access granted!         │
│ Redirect to dashboard   │
└─────────────────────────┘
```

---

### Flow 3: Widget Setup

```
┌───────────────────────────┐
│ Go to /dashboard/widget   │
└─────────────┬─────────────┘
              │
              ▼
┌───────────────────────────┐
│ Tab 1: Customize          │
│ • Choose colors           │
│ • Upload logo             │
│ • Set greeting            │
│ PATCH /widget/config      │
└─────────────┬─────────────┘
              │
              ▼
┌───────────────────────────┐
│ Tab 2: API Keys           │
│ • Create PUBLIC key       │
│ POST /api-keys            │
│ • Copy key (one-time!)    │
└─────────────┬─────────────┘
              │
              ▼
┌───────────────────────────┐
│ Tab 3: Installation       │
│ GET /widget/install-code  │
│ • Shows code snippet      │
│ • Copy to clipboard       │
└─────────────┬─────────────┘
              │
              ▼
┌───────────────────────────┐
│ Paste code on website     │
│ Before </body> tag        │
└─────────────┬─────────────┘
              │
              ▼
┌───────────────────────────┐
│ Widget appears on site!   │
│ • Fetches config from     │
│   GET /widget/public/...  │
│ • Applies branding        │
│ • Ready for chat          │
└───────────────────────────┘
```

---

## 13. Common Patterns

### Authentication Headers

**Platform Admin:**
```javascript
headers: {
  'Authorization': `Bearer ${platformAdminToken}`,
  'Content-Type': 'application/json'
}
```

**Tenant User:**
```javascript
headers: {
  'Authorization': `Bearer ${tenantToken}`,
  'Content-Type': 'application/json'
}
```

**Widget Public API:**
```javascript
headers: {
  'X-API-Key': publicApiKey,
  'Content-Type': 'application/json'
}
```

---

### Error Responses

All endpoints return errors in this format:

```json
{
  "detail": "Error message here"
}
```

**Common Status Codes:**
- `200` - Success
- `201` - Created
- `400` - Bad Request (validation error)
- `401` - Unauthorized (invalid/missing token)
- `403` - Forbidden (insufficient permissions)
- `404` - Not Found
- `500` - Internal Server Error

---

### Pagination (Future)

Currently, all list endpoints return full results. Pagination will be added later.

---

## 14. Frontend Pages Checklist

### Platform Admin Dashboard

- [ ] `/platform-admin/login` - Login page
- [ ] `/platform-admin/dashboard` - Main dashboard
- [ ] `/platform-admin/admins` - List/create admins (SUPER_ADMIN only)
- [ ] `/platform-admin/settings` - Change password

### Public Pages

- [ ] `/register` - Company registration
- [ ] `/verify-email` - Email verification
- [ ] `/resend-verification` - Resend verification email
- [ ] `/login` - Company login

### Company Dashboard

- [ ] `/dashboard` - Main dashboard
- [ ] `/dashboard/team` - Team management
  - List members
  - Invite member modal
  - Edit member modal
  - Regenerate password modal
- [ ] `/dashboard/settings/api-keys` - API key management
  - List keys
  - Create key modal (with one-time display)
  - Edit key modal
- [ ] `/dashboard/widget/setup` - Widget customization
  - Color pickers
  - Logo upload
  - Greeting message
  - Position selector
  - Live preview
- [ ] `/dashboard/widget/install` - Installation code
  - Code snippet with syntax highlighting
  - Copy button
  - Step-by-step instructions
- [ ] `/dashboard/knowledge` - Knowledge management
  - List all knowledge sources
  - Filter by status (active, processing, failed)
  - Search by name
  - View details (chunks preview)
  - Delete sources
- [ ] `/dashboard/knowledge/upload` - Upload knowledge
  - File upload (drag & drop)
  - Text input option
  - Source type selector (FAQ, Help Doc, Policy, SOP)
  - File format support (.txt, .md, .pdf, .docx)
  - Progress tracking for file uploads
- [ ] `/dashboard/knowledge/processing` - Upload job status
  - Progress bar
  - Real-time updates (WebSocket or polling)
  - Error handling
  - Completion notification
- [ ] `/dashboard/knowledge/{source_id}` - Knowledge source details
  - Metadata display
  - Chunks list with preview
  - Search within chunks
  - Download/export option
- [ ] `/dashboard/settings/password` - Change password

### Agent Support Dashboard

- [ ] `/dashboard/support/conversations` - Escalated conversations list
  - Filter by status (escalated, resolved, abandoned)
  - Sort by escalation time
  - "Take Over" button for unassigned sessions
  - "View Details" button
- [ ] `/dashboard/support/conversations/{session_id}` - Conversation details with real-time chat
  - Full message history with different styling per role
  - Session metadata sidebar
  - "Take Over" button if not assigned
  - Message input box (HTTP or WebSocket)
  - WebSocket integration for real-time updates
- [ ] `/dashboard/support/tickets` - Ticket management dashboard
  - List all tickets with filters (status, priority, assigned to me)
  - Search by ticket number or summary
  - Status and priority badges
  - Pagination controls
  - "Unassigned Tickets" filter
  - Actions: View Details, Assign, Resolve
- [ ] `/dashboard/support/tickets/{ticket_id}` - Ticket details
  - Full ticket information card
  - Link to associated conversation
  - Action buttons based on status (Assign, Resolve, Close)
  - Timeline showing ticket lifecycle
- [ ] `/dashboard/support/tickets/create` - Manual ticket creation
  - Session selector
  - Summary input
  - Priority selector
  - "Assign to yourself?" option
- [ ] `/dashboard/support/tickets/{ticket_id}/edit` - Edit ticket
  - Update priority
  - Change assignment
  - Add working notes
  - Update status

### Analytics & Review Dashboard

- [ ] `/dashboard/analytics/ratings` - Customer satisfaction ratings
  - Display average score with star rating UI
  - Score distribution chart (1-5 stars)
  - Filters: date range, score range, session
  - List view with pagination
  - Link ratings to conversation sessions
  - Export data functionality
  - Trends over time chart
- [ ] `/dashboard/feedback` - Submit platform feedback
  - Feedback form with category selector
  - Title and description fields
  - Conditional satisfaction score input (for platform_satisfaction)
  - Submit button with success message
- [ ] `/dashboard/feedback/history` - View submitted feedback
  - List all feedback with status badges
  - Filters by category and status
  - Search functionality
  - Detailed view modal/page for each feedback item
  - Status color coding (open, acknowledged, in_progress, resolved, closed)

### Widget Integration (Customer-Facing)

- [ ] Post-chat rating prompt in widget
  - 5-star rating UI
  - Optional comment field
  - Thank you message after submission
  - Handle duplicate rating gracefully
  - Trigger after conversation ends or satisfaction detected

---

## 15. Next Steps

After frontend is complete, the widget JavaScript and chat UI will be built separately.

**Widget SDK** (Separate project):
- JavaScript bundle that embeds on customer websites
- Loads configuration from `GET /widget/public/config/{tenant}`
- Creates floating button and chat window

**Chat UI** (Separate project):
- React/Vue app hosted at `chat.luranai.com/widget`
- Loaded in iframe by widget SDK
- Sends messages to chat API

---

## 16. Important Notes

1. **Two Separate Tokens:** Platform admin tokens and tenant tokens are different. Store them separately.

2. **API Key Security:**
   - Public keys (pk_live_...) are safe to expose in widget JavaScript
   - Secret keys (sk_live_...) must NEVER be exposed in frontend

3. **One-Time Display:** API keys' `full_key` is only shown once during creation. After that, only `key_prefix` is available.

4. **Email Verification:** New companies must verify email before they can log in.

5. **Role Restrictions:**
   - Only TENANT_ADMIN can invite members, manage API keys, configure widget
   - Only SUPER_ADMIN can create new platform admins

6. **Tenant Isolation:** All tenant endpoints automatically filter by the authenticated user's tenant_id. Users can only see/modify their own tenant's data.

7. **Knowledge Upload Formats:**
   - Both long-form documents and Q&A format are supported
   - File uploads: .txt, .md, .pdf, .docx (max 10MB)
   - Text uploads: Any length (minimum 10 characters)
   - Processing is asynchronous for file uploads (use job polling or WebSocket)
   - Embeddings are rate-limited at 10 requests/minute (Google AI)

8. **Agent Support Dashboard:**
   - Only SUPPORT_AGENT and TENANT_ADMIN roles can access agent endpoints
   - Agent takeover automatically assigns associated tickets
   - WebSocket is recommended for real-time chat (see WEBSOCKET_LIVE_CHAT_GUIDE.md)
   - HTTP agent-message endpoint available for non-real-time scenarios
   - Session status values: active, escalated, resolved, abandoned
   - Message role values: user, assistant, human_agent, system

9. **Ticket Management:**
   - Tickets must be resolved before they can be closed (two-step process)
   - Resolution notes are required when resolving tickets
   - Assign endpoint requires TENANT_ADMIN role
   - Resolve endpoint requires SUPPORT_AGENT or TENANT_ADMIN role
   - Close endpoint requires TENANT_ADMIN role
   - Ticket status: open → in_progress → resolved → closed
   - Priority levels: low, medium, high, urgent
   - Ticket numbers follow format: TKT-YYYY-NNNN

10. **Review & Rating System:**
   - Session ratings are public endpoints (no auth required)
   - Only one rating per session allowed (enforced by database constraint)
   - User identifier must match session for security
   - Platform feedback categories: feature_request, bug_report, performance, platform_satisfaction, documentation, pricing, general
   - satisfaction_score (1-5) is only required for platform_satisfaction category
   - Feedback status values: open, acknowledged, in_progress, resolved, closed
   - Average score is calculated server-side and included in list response
   - All review endpoints return tenant-scoped data automatically

---

**Questions?** Contact the backend team for clarification on any endpoints.

---

## Platform Admin Analytics & Monitoring

### Overview

The platform admin dashboard provides comprehensive analytics, monitoring, and management capabilities across all tenants. All endpoints require platform admin authentication via `/platform/auth/login`.

**Authentication:** All platform endpoints require `Authorization: Bearer <platform_admin_token>`

---

### 15. Platform Analytics

**Authentication:** All analytics endpoints require Platform Admin authentication with SUPER_ADMIN role.

**Base URL:** `/platform/analytics`

---

#### 15.1 Get Dashboard Summary

Get comprehensive platform analytics dashboard with all key metrics in one call.

**Endpoint:** `GET /platform/analytics/dashboard`

**Auth Required:** Yes (Platform Admin / SUPER_ADMIN)

**Query Parameters:** None

**Response:**
```json
{
  "revenue": {
    "mrr": 35000.0,
    "arr": 420000.0,
    "mrr_growth_rate": 17.647058823529413,
    "revenue_by_tier": {
      "lite": 0.0,
      "professional": 35000.0
    },
    "currency": "NGN",
    "period": {
      "start": "2025-12-28T21:53:00.049585",
      "end": "2026-01-27T21:53:00.049585"
    }
  },
  "subscriptions": {
    "active_subscriptions": 1,
    "trialing_tenants": 0,
    "inactive_tenants": 0,
    "trial_conversion_rate": 100.0,
    "churn_rate": 0.0,
    "churned_count": 0,
    "converted_count": 1,
    "tier_distribution": {
      "professional": 1
    },
    "period_days": 30
  },
  "growth": {
    "new_signups": 1,
    "upgrades": 1,
    "downgrades": 0,
    "signup_growth_rate": 100.0,
    "daily_trend": [
      {"date": "2026-01-21", "count": 0},
      {"date": "2026-01-22", "count": 0},
      {"date": "2026-01-23", "count": 0},
      {"date": "2026-01-24", "count": 0},
      {"date": "2026-01-25", "count": 0},
      {"date": "2026-01-26", "count": 1},
      {"date": "2026-01-27", "count": 0}
    ],
    "period_days": 7
  },
  "usage": {
    "total_conversations": 42,
    "active_conversations_30d": 35,
    "total_tickets": 8,
    "active_tickets": 2,
    "total_knowledge_sources": 15,
    "avg_conversations_per_tenant": 42.0,
    "active_tenants": 1
  },
  "generated_at": "2026-01-27T21:53:00.123456"
}
```

**Use Case:** Primary dashboard endpoint - displays all key platform metrics at once for admin overview.

---

#### 15.2 Get Revenue Metrics

Get platform-wide revenue metrics including MRR, ARR, and growth rates.

**Endpoint:** `GET /platform/analytics/revenue`

**Auth Required:** Yes (Platform Admin / SUPER_ADMIN)

**Query Parameters:**
- `start_date` (optional): Start date in ISO format (defaults to 30 days ago)
- `end_date` (optional): End date in ISO format (defaults to now)

**Response:**
```json
{
  "mrr": 35000.0,
  "arr": 420000.0,
  "mrr_growth_rate": 17.647058823529413,
  "revenue_by_tier": {
    "lite": 0.0,
    "professional": 35000.0
  },
  "currency": "NGN",
  "period": {
    "start": "2025-12-28T00:00:00",
    "end": "2026-01-27T00:00:00"
  }
}
```

**Field Descriptions:**
- `mrr`: Monthly Recurring Revenue in Naira (sum of all active monthly subscriptions + annualized monthly equivalent of annual subscriptions)
- `arr`: Annual Recurring Revenue (MRR × 12)
- `mrr_growth_rate`: Percentage growth compared to previous period
- `revenue_by_tier`: MRR breakdown by subscription tier

#### 15.2 Get Subscription Metrics

Get subscription health metrics including churn and conversion rates.

**Endpoint:** `GET /platform/analytics/subscriptions`

**Query Parameters:**
- `days` (optional): Number of days to analyze (default: 30, max: 365)

**Response:**
```json
{
  "active_subscriptions": 145,
  "trialing_tenants": 52,
  "inactive_tenants": 18,
  "trial_conversion_rate": 68.5,
  "churn_rate": 4.2,
  "churned_count": 6,
  "converted_count": 24,
  "tier_distribution": {
    "starter": 65,
    "professional": 72,
    "enterprise": 8
  },
  "period_days": 30
}
```

#### 15.3 Get Growth Metrics

Get platform growth trends including signups and upgrades.

**Endpoint:** `GET /platform/analytics/growth`

**Query Parameters:**
- `days` (optional): Number of days to analyze (default: 30)

**Response:**
```json
{
  "new_signups": 38,
  "upgrades": 24,
  "downgrades": 2,
  "signup_growth_rate": 22.5,
  "daily_trend": [
    {"date": "2026-01-21", "count": 5},
    {"date": "2026-01-22", "count": 7},
    {"date": "2026-01-23", "count": 3}
  ],
  "period_days": 30
}
```

#### 15.4 Get Usage Metrics

Get platform-wide usage statistics.

**Endpoint:** `GET /platform/analytics/usage`

**Response:**
```json
{
  "total_conversations": 15847,
  "active_conversations_30d": 3254,
  "total_tickets": 1254,
  "active_tickets": 87,
  "total_knowledge_sources": 524,
  "avg_conversations_per_tenant": 22.4,
  "active_tenants": 145
}
```

#### 15.5 Get Cohort Analysis

Analyze tenant retention by signup cohort.

**Endpoint:** `GET /platform/analytics/cohorts`

**Query Parameters:**
- `cohort_size` (optional): "week" or "month" (default: "month")
- `months_back` (optional): Number of months to analyze (default: 6, max: 24)

**Response:**
```json
{
  "cohorts": [
    {
      "cohort": "2025-12",
      "total_signups": 42,
      "active_count": 38,
      "churned_count": 4,
      "retention_rate": 90.5,
      "churn_rate": 9.5
    }
  ],
  "cohort_size": "month",
  "months_analyzed": 6,
  "period": {
    "start": "2025-07-27T00:00:00",
    "end": "2026-01-27T00:00:00"
  }
}
```

#### 15.6 Get Dashboard Summary

Get comprehensive dashboard with all metrics.

**Endpoint:** `GET /platform/analytics/dashboard`

**Response:** Combined response from all analytics endpoints above.

---

### 16. System Health Monitoring

#### 16.1 Get Background Job Health

Monitor ARQ background job status and failures.

**Endpoint:** `GET /platform/health/jobs`

**Query Parameters:**
- `hours` (optional): Hours to analyze (default: 24, max: 168)

**Response:**
```json
{
  "period_hours": 24,
  "status_counts": {
    "pending": 5,
    "processing": 2,
    "completed": 143,
    "failed": 3
  },
  "success_rate": 97.9,
  "jobs_by_type": {
    "document_ingestion": 120,
    "trial_expiration_check": 15,
    "subscription_confirmations": 13
  },
  "recent_failures": [
    {
      "id": "uuid",
      "job_type": "document_ingestion",
      "tenant_id": "uuid",
      "error_message": "Rate limit exceeded",
      "created_at": "2026-01-27T10:30:00"
    }
  ],
  "health_status": "healthy"
}
```

#### 16.2 Get API Health

Monitor API performance from audit logs.

**Endpoint:** `GET /platform/health/api`

**Query Parameters:**
- `hours` (optional): Hours to analyze (default: 1, max: 24)

**Response:**
```json
{
  "period_hours": 1,
  "total_requests": 2547,
  "requests_per_minute": 42.45,
  "error_rate": 0.8,
  "top_actions": [
    {"action": "chat_message", "count": 854},
    {"action": "knowledge_search", "count": 432}
  ],
  "health_status": "healthy"
}
```

#### 16.3 Get Database Health

Monitor database connection pool and performance.

**Endpoint:** `GET /platform/health/database`

**Response:**
```json
{
  "connection_pool": {
    "size": 20,
    "checked_in": 17,
    "checked_out": 3,
    "overflow": 0,
    "max_overflow": 40
  },
  "pool_utilization_percent": 15.0,
  "response_time_ms": 12.5,
  "database_size_mb": 2548.7,
  "record_counts": {
    "tenants": 197,
    "users": 542,
    "conversations": 15847
  },
  "health_status": "healthy"
}
```

#### 16.4 Get Queue Health

Monitor Redis/ARQ queue status.

**Endpoint:** `GET /platform/health/queue`

**Response:**
```json
{
  "redis_ping_ms": 2.3,
  "queue_length": 12,
  "used_memory_mb": 145.8,
  "memory_utilization_percent": 14.6,
  "connected_clients": 8,
  "failed_jobs_cached": 2,
  "uptime_seconds": 86400,
  "health_status": "healthy"
}
```

#### 16.5 Get Overall Health

Get comprehensive system health summary.

**Endpoint:** `GET /platform/health/overall`

**Response:**
```json
{
  "overall_status": "healthy",
  "timestamp": "2026-01-27T12:00:00",
  "components": {
    "background_jobs": {...},
    "api": {...},
    "database": {...},
    "queue": {...}
  }
}
```

---

### 17. Financial Reporting

#### 17.1 Get Revenue Breakdown

Detailed revenue analysis by tier and billing cycle.

**Endpoint:** `GET /platform/financials/revenue-breakdown`

**Query Parameters:**
- `start_date` (optional): ISO format
- `end_date` (optional): ISO format

**Response:**
```json
{
  "period": {
    "start": "2026-01-01T00:00:00",
    "end": "2026-01-27T00:00:00",
    "days": 27
  },
  "total_revenue": 6750000.0,
  "total_payments": 156,
  "average_revenue_per_payment": 43269.23,
  "revenue_by_tier": {
    "starter": 1950000.0,
    "professional": 4125000.0,
    "enterprise": 675000.0
  },
  "payment_counts_by_tier": {
    "starter": 78,
    "professional": 55,
    "enterprise": 23
  },
  "revenue_by_billing_cycle": {
    "monthly": 5400000.0,
    "annual": 1350000.0
  },
  "daily_revenue": {
    "2026-01-01": 250000.0,
    "2026-01-02": 175000.0
  },
  "currency": "NGN"
}
```

#### 17.2 Get Payment Analytics

Payment success/failure analysis.

**Endpoint:** `GET /platform/financials/payment-analytics`

**Query Parameters:**
- `days` (optional): Days to analyze (default: 30, max: 365)

**Response:**
```json
{
  "period_days": 30,
  "total_attempts": 178,
  "status_counts": {
    "success": 156,
    "failed": 18,
    "pending": 4
  },
  "success_rate": 87.6,
  "successful_revenue": 6750000.0,
  "potential_lost_revenue": 780000.0,
  "payment_methods": {
    "card": {
      "count": 178,
      "success": 156,
      "failed": 18,
      "success_rate": 87.6
    }
  },
  "recent_failures": [],
  "currency": "NGN"
}
```

#### 17.3 Get Subscription Forecast

Revenue forecast based on current trends.

**Endpoint:** `GET /platform/financials/forecast`

**Query Parameters:**
- `months` (optional): Months to forecast (default: 6, max: 24)

**Response:**
```json
{
  "current_mrr": 2500000.0,
  "current_arr": 30000000.0,
  "monthly_growth_rate": 8.5,
  "forecast_months": 6,
  "forecast": [
    {
      "month": 1,
      "date": "2026-02",
      "projected_mrr": 2712500.0,
      "projected_arr": 32550000.0
    }
  ],
  "total_projected_revenue": 17437500.0,
  "currency": "NGN",
  "disclaimer": "Forecast based on current trends and may vary with market conditions"
}
```

#### 17.4 Get Refund Report

Refund tracking and analysis.

**Endpoint:** `GET /platform/financials/refunds`

**Query Parameters:**
- `start_date` (optional): ISO format
- `end_date` (optional): ISO format

**Response:**
```json
{
  "period": {
    "start": "2026-01-01T00:00:00",
    "end": "2026-01-27T00:00:00"
  },
  "total_refunds": 12,
  "refunds_by_tier": {
    "starter": 7,
    "professional": 4,
    "enterprise": 1
  },
  "refunds_by_reason": {
    "service_issues": 5,
    "switching_platforms": 4,
    "budget_constraints": 3
  },
  "recent_refunds": []
}
```

#### 17.5 Get Financial Summary

Comprehensive financial overview.

**Endpoint:** `GET /platform/financials/summary`

**Response:** Combined response from all financial endpoints.

---

### 18. Audit Log Management

#### 18.1 Search Audit Logs

Search and filter platform audit logs.

**Endpoint:** `GET /platform/audit/logs`

**Query Parameters:**
- `tenant_id` (optional): Filter by tenant UUID
- `user_id` (optional): Filter by user UUID
- `action` (optional): Filter by action (partial match)
- `resource_type` (optional): Filter by resource type
- `start_date` (optional): ISO format
- `end_date` (optional): ISO format
- `limit` (optional): Max results (default: 100, max: 1000)
- `offset` (optional): Results offset (default: 0)

**Response:**
```json
{
  "logs": [
    {
      "id": "uuid",
      "tenant_id": "uuid",
      "user_id": "uuid",
      "action": "subscription_override",
      "resource_type": "tenant",
      "resource_id": "uuid",
      "changes": {
        "old_tier": "trial",
        "new_tier": "professional"
      },
      "ip_address": "192.168.1.1",
      "user_agent": "Mozilla/5.0...",
      "created_at": "2026-01-27T10:30:00"
    }
  ],
  "pagination": {
    "total": 5420,
    "limit": 100,
    "offset": 0,
    "has_more": true
  }
}
```

#### 18.2 Export Audit Logs

Export audit logs to CSV or JSON.

**Endpoint:** `GET /platform/audit/logs/export`

**Query Parameters:**
- `format`: "csv" or "json" (required)
- `tenant_id` (optional): Filter by tenant
- `start_date` (optional): ISO format
- `end_date` (optional): ISO format

**Response:** File download (CSV or JSON)

**Headers:**
```
Content-Type: text/csv (or application/json)
Content-Disposition: attachment; filename=audit_logs_20260127_120000.csv
```

#### 18.3 Get Tenant Activity

Activity summary for a specific tenant.

**Endpoint:** `GET /platform/audit/tenant/{tenant_id}/activity`

**Path Parameters:**
- `tenant_id`: Tenant UUID

**Query Parameters:**
- `days` (optional): Days to analyze (default: 30, max: 365)

**Response:**
```json
{
  "tenant_id": "uuid",
  "tenant_name": "acme_corp",
  "period_days": 30,
  "total_actions": 1254,
  "active_users": 12,
  "actions_breakdown": {
    "chat_message": 854,
    "knowledge_upload": 42,
    "user_invited": 8
  },
  "daily_activity": {
    "2026-01-27": 48,
    "2026-01-26": 52
  }
}
```

#### 18.4 Get Security Events

Security-related events with severity classification.

**Endpoint:** `GET /platform/audit/security-events`

**Query Parameters:**
- `severity` (optional): "high", "medium", or "low"
- `days` (optional): Days to analyze (default: 7, max: 90)
- `limit` (optional): Max results (default: 100, max: 1000)

**Response:**
```json
{
  "period_days": 7,
  "total_events": 24,
  "severity_counts": {
    "high": 3,
    "medium": 12,
    "low": 9
  },
  "events": [
    {
      "id": "uuid",
      "severity": "high",
      "action": "user_deactivated",
      "tenant_id": "uuid",
      "user_id": "uuid",
      "resource_type": "user",
      "changes": {...},
      "ip_address": "192.168.1.1",
      "created_at": "2026-01-27T10:30:00"
    }
  ]
}
```

---

### 19. Review & Feedback Management

#### 19.1 Get All Session Ratings

Cross-tenant session rating analytics.

**Endpoint:** `GET /platform/reviews/ratings/all`

**Query Parameters:**
- `tenant_id` (optional): Filter by tenant UUID
- `min_score` (optional): Min score (1-5)
- `max_score` (optional): Max score (1-5)
- `days` (optional): Days to analyze (default: 30, max: 365)
- `limit` (optional): Max results (default: 100, max: 1000)
- `offset` (optional): Results offset

**Response:**
```json
{
  "ratings": [
    {
      "id": "uuid",
      "tenant_id": "uuid",
      "session_id": "uuid",
      "score": 5,
      "comment": "Excellent service!",
      "user_identifier": "user_12345",
      "created_at": "2026-01-27T10:30:00"
    }
  ],
  "statistics": {
    "total_ratings": 2547,
    "average_score": 4.2,
    "score_distribution": {
      "5": 1254,
      "4": 854,
      "3": 287,
      "2": 102,
      "1": 50
    }
  },
  "pagination": {
    "limit": 100,
    "offset": 0,
    "total": 2547
  }
}
```

#### 19.2 Analyze Sentiment

Sentiment analysis from ratings and feedback.

**Endpoint:** `GET /platform/reviews/sentiment-analysis`

**Query Parameters:**
- `tenant_id` (optional): Filter by tenant UUID
- `days` (optional): Days to analyze (default: 30, max: 365)

**Response:**
```json
{
  "period_days": 30,
  "session_ratings": {
    "total": 2547,
    "positive": 2108,
    "neutral": 287,
    "negative": 152,
    "positive_percentage": 82.8
  },
  "platform_feedback": {
    "satisfied": 184,
    "unsatisfied": 24
  },
  "overall_sentiment_score": 78.5,
  "sentiment_category": "positive"
}
```

#### 19.3 Get Tenant Health Scores

Calculate tenant health based on feedback.

**Endpoint:** `GET /platform/reviews/tenant-health-scores`

**Query Parameters:**
- `days` (optional): Days to analyze (default: 30, max: 365)
- `limit` (optional): Max tenants (default: 50, max: 200)

**Response:**
```json
{
  "period_days": 30,
  "total_tenants": 145,
  "tenants": [
    {
      "tenant_id": "uuid",
      "tenant_name": "acme_corp",
      "display_name": "Acme Corporation",
      "health_score": 85.4,
      "health_category": "excellent",
      "metrics": {
        "average_rating": 4.5,
        "rating_count": 142,
        "average_satisfaction": 4.2
      }
    }
  ]
}
```

#### 19.4 Get Platform Feedback

Cross-tenant platform feedback.

**Endpoint:** `GET /platform/reviews/feedback/platform`

**Query Parameters:**
- `tenant_id` (optional): Filter by tenant UUID
- `category` (optional): Feedback category
- `status` (optional): Feedback status
- `days` (optional): Days to analyze (default: 30, max: 365)
- `limit` (optional): Max results (default: 100, max: 1000)
- `offset` (optional): Results offset

**Response:**
```json
{
  "feedback": [
    {
      "id": "uuid",
      "tenant_id": "uuid",
      "category": "feature_request",
      "title": "Add bulk upload feature",
      "description": "...",
      "satisfaction_score": 4,
      "status": "acknowledged",
      "created_at": "2026-01-27T10:30:00"
    }
  ],
  "total": 208,
  "page": 1,
  "page_size": 100
}
```

---

### 20. User Management

#### 20.1 Search Users

Cross-tenant user search.

**Endpoint:** `GET /platform/users/search`

**Query Parameters:**
- `email` (optional): Email filter (partial match)
- `tenant_id` (optional): Filter by tenant UUID
- `role` (optional): "tenant_admin", "support_agent", or "viewer"
- `is_active` (optional): Active status filter (true/false)
- `limit` (optional): Max results (default: 100, max: 1000)
- `offset` (optional): Results offset

**Response:**
```json
{
  "users": [
    {
      "id": "uuid",
      "email": "john@acme.com",
      "full_name": "John Doe",
      "role": "tenant_admin",
      "is_active": true,
      "is_verified": true,
      "tenant_id": "uuid",
      "tenant_name": "acme_corp",
      "tenant_display_name": "Acme Corporation",
      "created_at": "2025-12-15T10:00:00",
      "updated_at": "2026-01-27T09:00:00",
      "last_login": "2026-01-27T08:30:00"
    }
  ],
  "pagination": {
    "total": 542,
    "limit": 100,
    "offset": 0,
    "has_more": true
  }
}
```

#### 20.2 Get User Details

Detailed user information with activity.

**Endpoint:** `GET /platform/users/{user_id}`

**Path Parameters:**
- `user_id`: User UUID

**Response:**
```json
{
  "id": "uuid",
  "email": "john@acme.com",
  "full_name": "John Doe",
  "role": "tenant_admin",
  "is_active": true,
  "is_verified": true,
  "tenant": {
    "id": "uuid",
    "name": "acme_corp",
    "display_name": "Acme Corporation",
    "subscription_status": "active",
    "subscription_tier": "professional"
  },
  "created_at": "2025-12-15T10:00:00",
  "updated_at": "2026-01-27T09:00:00",
  "last_login": "2026-01-27T08:30:00",
  "recent_activity": {
    "period_days": 30,
    "total_actions": 245,
    "actions_breakdown": {...},
    "recent_actions": [...]
  }
}
```

#### 20.3 Get User Activity

User activity summary from audit logs.

**Endpoint:** `GET /platform/users/{user_id}/activity`

**Path Parameters:**
- `user_id`: User UUID

**Query Parameters:**
- `days` (optional): Days to analyze (default: 30, max: 365)

**Response:**
```json
{
  "period_days": 30,
  "total_actions": 245,
  "actions_breakdown": {
    "chat_message": 154,
    "knowledge_upload": 12,
    "widget_config_update": 3
  },
  "recent_actions": [
    {
      "action": "chat_message",
      "resource_type": "conversation",
      "created_at": "2026-01-27T10:30:00"
    }
  ]
}
```

#### 20.4 Deactivate User

Deactivate a user account (requires platform admin).

**Endpoint:** `POST /platform/users/{user_id}/deactivate`

**Path Parameters:**
- `user_id`: User UUID

**Request Body:**
```json
{
  "reason": "Policy violation - spam detected"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "User john@acme.com has been deactivated",
  "user_id": "uuid",
  "reason": "Policy violation - spam detected",
  "deactivated_at": "2026-01-27T12:00:00"
}
```

#### 20.5 Get Role Distribution

User role distribution statistics.

**Endpoint:** `GET /platform/users/analytics/role-distribution`

**Response:**
```json
{
  "total_users": 542,
  "active_users": 487,
  "inactive_users": 55,
  "verified_users": 524,
  "role_distribution": {
    "tenant_admin": 197,
    "support_agent": 284,
    "viewer": 61
  },
  "recent_signups_30d": 38
}
```

---

### Health Status Values

All health endpoints return one of these statuses:

- **healthy**: System operating normally
- **degraded**: System functional but experiencing issues
- **unhealthy**: Critical issues requiring immediate attention
- **unavailable**: Component not accessible

---

### Platform Admin Frontend Pages Checklist

#### Analytics Dashboard
- [ ] Revenue overview cards (MRR, ARR, growth rate)
- [ ] Subscription health metrics (churn, conversion, active/trial counts)
- [ ] Growth charts (daily signups, upgrades, trends)
- [ ] Usage statistics (conversations, tickets, knowledge sources)
- [ ] Cohort retention table
- [ ] Date range selector

#### System Health Monitor
- [ ] Overall health status indicator
- [ ] Background jobs panel (success rate, recent failures)
- [ ] API health panel (request rate, error rate)
- [ ] Database health panel (connection pool, response time)
- [ ] Queue health panel (backlog, memory usage)
- [ ] Real-time refresh toggle

#### Financial Reports
- [ ] Revenue breakdown table (by tier and cycle)
- [ ] Payment analytics (success rate, failure reasons)
- [ ] Revenue forecast chart (6-month projection)
- [ ] Refund tracking table
- [ ] Export to Excel/CSV button

#### Audit Logs
- [ ] Search filters (tenant, user, action, date range)
- [ ] Audit log table with sorting
- [ ] Export to CSV/JSON button
- [ ] Tenant activity detail modal
- [ ] Security events panel with severity indicators

#### Review Management
- [ ] Session ratings table with filters
- [ ] Sentiment analysis dashboard
- [ ] Tenant health scores ranking
- [ ] Platform feedback management
- [ ] Feedback status update

#### User Management
- [ ] User search with multiple filters
- [ ] User details modal with activity timeline
- [ ] Deactivate user confirmation dialog
- [ ] Role distribution pie chart
- [ ] Bulk actions support

---

### Important Notes for Platform Admin Features

1. **Authentication:** All platform admin endpoints require `Bearer <platform_admin_token>` from `/platform/auth/login`

2. **Rate Limiting:** Platform admin endpoints have higher rate limits (100 req/min) compared to tenant endpoints

3. **Pagination:** Most list endpoints support limit/offset pagination with max 1000 results per request

4. **Performance:** Analytics queries are optimized but may take 2-5 seconds for large datasets. Consider implementing loading states.

5. **Export Limits:** Audit log exports are limited to 10,000 records per request. Use date range filters for larger exports.

6. **Health Monitoring:** System health endpoints are designed for polling every 30-60 seconds. More frequent polling may impact performance.

7. **Financial Data:** All financial amounts are in Nigerian Naira (NGN). MRR calculations include annual subscriptions divided by 12.

8. **Security Events:** High severity events include admin creation/deletion, user deactivation, and subscription overrides. These should trigger alerts in the frontend.

9. **Tenant Health Scores:** Calculated using 60% session ratings + 40% platform feedback satisfaction. Scores below 40 indicate at-risk tenants.

10. **User Deactivation:** Deactivating a user logs the action in audit trails and notifies the tenant admin automatically (via email notification service).

---

**Last Updated:** 2026-01-27  
**Backend Team Contact:** For questions about platform admin features
