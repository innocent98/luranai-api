# Frontend Development Guide - Complete API Reference

**Last Updated:** 2026-01-18
**Base URL:** `http://localhost:8000/api/v1` (development) | `https://api.luran.com/api/v1` (production)

## Table of Contents

1. [Authentication Overview](#authentication-overview)
2. [Platform Admin Dashboard](#platform-admin-dashboard)
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
   - WebSocket Real-Time Chat
9. [Ticket Management](#ticket-management)
   - List Tickets
   - Get Ticket Details
   - Create Ticket Manually
   - Assign Ticket
   - Update Ticket
   - Resolve Ticket
   - Close Ticket
   - Get Unassigned Tickets
10. [User Flows with Diagrams](#user-flows-with-diagrams)
11. [Common Patterns](#common-patterns)
12. [Frontend Pages Checklist](#frontend-pages-checklist)
13. [Next Steps](#next-steps)
14. [Important Notes](#important-notes)

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
  "email": "admin@luran.dev",
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
  "email": "admin@luran.dev",
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
  "email": "newadmin@luran.dev",
  "full_name": "New Admin",
  "role": "ADMIN",
  "password": "secure-password"
}
```

**Response:**
```json
{
  "id": "uuid",
  "email": "newadmin@luran.dev",
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
    "email": "admin@luran.dev",
    "full_name": "Admin User",
    "role": "SUPER_ADMIN",
    "is_active": true,
    "created_at": "2025-01-01T00:00:00Z"
  },
  {
    "id": "uuid",
    "email": "john@luran.dev",
    "full_name": "John Smith",
    "role": "ADMIN",
    "is_active": true,
    "created_at": "2026-01-10T00:00:00Z"
  }
]
```

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
- Email: support@luran.com
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

### 5.2 Update Widget Configuration

**Page:** `/dashboard/widget/setup`

**Endpoint:** `PATCH /widget/config`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

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
- File upload for logo
- Textarea for greeting message
- Dropdown for position

---

### 5.3 Get Installation Code

**Page:** `/dashboard/widget/install`

**Endpoint:** `GET /widget/install-code`

**Headers:**
```
Authorization: Bearer {tenant_token}
```

**Response:**
```json
{
  "html_snippet": "<!-- Luran Widget Integration -->\n<script>\n  window.luranSettings = {\n    tenantId: 'acme_corp',\n    apiKey: 'YOUR_PUBLIC_API_KEY_HERE'\n  };\n</script>\n<script src=\"https://cdn.luran.com/widget/v1/widget.js\" async defer></script>\n<!-- End Luran Widget -->",
  "instructions": "Installation Instructions:\n\n1. Get your Public API Key:\n   - Go to Settings → API Keys\n   - Create a new PUBLIC API key\n   - Copy the key (starts with pk_live_...)\n\n2. Replace 'YOUR_PUBLIC_API_KEY_HERE' in the code above with your actual public API key\n\n3. Add the code snippet to your website:\n   - Paste it before the closing </body> tag\n   - The widget will appear on all pages where this code is present\n\n4. Customize the widget:\n   - Go to Settings → Widget Configuration\n   - Adjust colors, logo, greeting message, and position\n\n5. Test the widget:\n   - Visit your website\n   - Click the floating chat button\n   - Start a conversation\n\nNote: The widget loads asynchronously and won't affect your page load speed."
}
```

**Frontend:**
- Display code in syntax-highlighted code block
- Copy-to-clipboard button
- Step-by-step installation instructions
- "Test Widget" button that opens preview

---

## 6. Widget Public API (No Auth Required)

### 6.1 Get Widget Configuration (Public)

**Used by:** Embedded widget JavaScript

**Endpoint:** `GET /widget/public/config/{tenant_name}`

**Headers:**
```
X-API-Key: pk_live_abc123...
```

**Example:**
```
GET /widget/public/config/acme_corp
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

**WebSocket Endpoint:** `wss://api.luran.com/api/v1/ws/agent/{session_id}`

**Connection:**
```javascript
// Connect to agent WebSocket
const ws = new WebSocket(
  `wss://api.luran.com/api/v1/ws/agent/${sessionId}?token=${tenantToken}`
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

**For Complete WebSocket Documentation:**
See `/docs/WEBSOCKET_LIVE_CHAT_GUIDE.md` for:
- Full message format specifications
- Connection lifecycle details
- Error handling patterns
- Customer-side WebSocket integration
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
  "resolution_notes": null,
  "resolved_at": null,
  "created_at": "2026-01-18T10:30:00Z",
  "updated_at": "2026-01-18T11:15:00Z"
}
```

**Display:**
- Full ticket details card showing all fields
- Link to associated conversation: "View Conversation" button → `/dashboard/support/conversations/{session_id}`
- Assigned agent name (fetch from user endpoint if needed)
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

## 10. User Flows with Diagrams

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

## 11. Common Patterns

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

## 12. Frontend Pages Checklist

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

---

## 13. Next Steps

After frontend is complete, the widget JavaScript and chat UI will be built separately.

**Widget SDK** (Separate project):
- JavaScript bundle that embeds on customer websites
- Loads configuration from `GET /widget/public/config/{tenant}`
- Creates floating button and chat window

**Chat UI** (Separate project):
- React/Vue app hosted at `chat.luran.com/widget`
- Loaded in iframe by widget SDK
- Sends messages to chat API

---

## 14. Important Notes

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

---

**Questions?** Contact the backend team for clarification on any endpoints.
