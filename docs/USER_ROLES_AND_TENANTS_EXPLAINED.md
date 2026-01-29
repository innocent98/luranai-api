# User Roles and Tenants - Complete Explanation

## Overview

Luran is a **multi-tenant SaaS platform**. This means multiple companies (tenants) can use the same system, but their data is completely isolated from each other.

---

## 🏢 What is a Tenant?

A **tenant** is a company/product using your AI support platform.

### Real Examples in Your Database

```sql
-- Current tenants:
1. Weysure (bb24bf9e-63c2-4497-b5f5-e46dd916f7ea)
   - An insurance company
   - Has their own knowledge base
   - Has their own AI configuration
   - Has their own conversations

2. Test Tenant (550e8400-e29b-41d4-a716-446655440000)
   - A demo/test tenant
   - Isolated from Weysure completely
```

### Tenant Data

```python
class Tenant:
    id: UUID                    # bb24bf9e-63c2-4497-b5f5-e46dd916f7ea
    name: str                   # "weysure" (unique, used in URLs)
    display_name: str           # "Weysure Insurance" (friendly name)
    description: str            # "Insurance company..."
    status: TenantStatus        # ACTIVE, INACTIVE, or SUSPENDED
    api_key_hash: str           # For API authentication

    # Relationships (owned by this tenant):
    - ai_config                 # Brand voice, escalation rules
    - capabilities              # What features they can use
    - knowledge_sources         # Uploaded documents
    - knowledge_chunks          # Searchable text chunks
    - conversation_sessions     # Customer chat sessions
    - messages                  # All chat messages
    - tickets                   # Support tickets
```

### Tenant Isolation

**Critical Concept:** Each tenant's data is COMPLETELY isolated.

```
Weysure's data:
├─ Knowledge: 13 chunks about insurance policies
├─ Conversations: 50 chat sessions with their customers
├─ Tickets: 5 escalated support tickets
└─ AI Config: Professional tone, insurance topics only

AegisScribe's data (different tenant):
├─ Knowledge: 20 chunks about legal documents
├─ Conversations: 100 chat sessions with their customers
├─ Tickets: 10 escalated tickets
└─ AI Config: Formal tone, legal topics only
```

**Weysure CANNOT see AegisScribe's data** - it's impossible because all queries filter by `tenant_id`.

---

## 👤 What is a User?

A **user** is a person who manages the platform - NOT the end customers chatting with the AI.

### User vs Customer

```
❌ NOT a user: John Smith asking "What is your refund policy?"
   → This is an anonymous customer (tracked by user_identifier)

✅ IS a user: Sarah Johnson, Weysure's support manager
   → She logs in to upload knowledge, view tickets, manage settings
```

### User Data

```python
class User:
    id: UUID                    # 910ca03c-3663-45a1-97bf-21eab2463154
    email: str                  # "admin@weysure.com" (unique)
    hashed_password: str        # Bcrypt hashed password
    full_name: str              # "Weysure Admin"
    role: UserRole              # SUPER_ADMIN, TENANT_ADMIN, SUPPORT_AGENT, or VIEWER
    tenant_id: UUID or None     # Which tenant they belong to (None for super admins)
    is_active: bool             # Can they log in?
    is_superuser: bool          # Platform-wide admin?
    last_login_at: DateTime     # Track activity
```

---

## 🎭 The 4 User Roles

### 1. SUPER_ADMIN (Platform Owner)

**Who:** You, the platform owner
**tenant_id:** NULL (belongs to no specific tenant)
**is_superuser:** True
**Access:** EVERYTHING across ALL tenants

```python
# Current super admin in your database:
{
    "id": "328b71d4-0c6c-46d5-b351-c67ff5bd7609",
    "email": "admin@luranai.com",
    "full_name": "Super Admin",
    "role": "SUPER_ADMIN",
    "tenant_id": None,          # ← No tenant!
    "is_superuser": True
}
```

**What they can do:**
- ✅ Create new tenants (companies)
- ✅ View all tenants' data
- ✅ Manage billing and subscriptions
- ✅ Access Weysure's knowledge base
- ✅ Access AegisScribe's knowledge base
- ✅ See all conversations across all tenants
- ✅ Suspend/activate tenants
- ✅ Create users for any tenant
- ✅ Platform-wide analytics

**Real-world example:**
You log in as admin@luranai.com and can see:
- Weysure has 50 conversations today
- AegisScribe has 100 conversations today
- Total platform usage statistics
- All tenants' knowledge bases

---

### 2. TENANT_ADMIN (Company Admin)

**Who:** The administrator of a specific company
**tenant_id:** Specific tenant UUID
**is_superuser:** False
**Access:** Only their own tenant's data

```python
# Current tenant admin in your database:
{
    "id": "910ca03c-3663-45a1-97bf-21eab2463154",
    "email": "admin@weysure.com",
    "full_name": "Weysure Admin",
    "role": "TENANT_ADMIN",
    "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",  # ← Weysure only!
    "is_superuser": False
}
```

**What they can do:**
- ✅ Upload knowledge documents for their company
- ✅ Configure AI settings (brand voice, escalation rules)
- ✅ View their company's conversations
- ✅ Manage their company's tickets
- ✅ Create support agents for their company
- ✅ View analytics for their company
- ❌ CANNOT see other tenants' data
- ❌ CANNOT create new tenants
- ❌ CANNOT access platform-wide settings

**Real-world example:**
Sarah logs in as admin@weysure.com and can:
- Upload Weysure's insurance FAQ documents
- See Weysure's 50 conversations today
- Manage Weysure's support tickets
- **CANNOT** see AegisScribe's data at all

---

### 3. SUPPORT_AGENT (Support Staff)

**Who:** Customer support team members
**tenant_id:** Specific tenant UUID
**is_superuser:** False
**Access:** Limited to viewing and responding to tickets

```python
# Example support agent:
{
    "id": "abc123...",
    "email": "sarah@weysure.com",
    "full_name": "Sarah Johnson",
    "role": "SUPPORT_AGENT",
    "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",  # Weysure
    "is_superuser": False
}
```

**What they can do:**
- ✅ View conversations for their company
- ✅ Respond to escalated tickets
- ✅ Assign tickets to themselves
- ✅ Mark tickets as resolved
- ✅ View knowledge base (read-only)
- ❌ CANNOT upload knowledge documents
- ❌ CANNOT change AI configuration
- ❌ CANNOT manage users
- ❌ CANNOT see other tenants' data

**Real-world example:**
Sarah logs in and sees:
- 5 escalated tickets needing her attention
- Conversation history for each ticket
- Can reply and resolve tickets
- Cannot change AI settings or upload documents

---

### 4. VIEWER (Read-Only)

**Who:** Managers, stakeholders, analysts
**tenant_id:** Specific tenant UUID
**is_superuser:** False
**Access:** Read-only access to their tenant's data

```python
# Example viewer:
{
    "id": "xyz789...",
    "email": "ceo@weysure.com",
    "full_name": "John Doe",
    "role": "VIEWER",
    "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",
    "is_superuser": False
}
```

**What they can do:**
- ✅ View conversations
- ✅ View tickets
- ✅ View analytics dashboards
- ✅ View knowledge base
- ❌ CANNOT make any changes
- ❌ CANNOT upload documents
- ❌ CANNOT respond to tickets
- ❌ CANNOT manage settings

**Real-world example:**
The CEO logs in to see:
- How many conversations happened today
- What issues customers are asking about
- Response quality metrics
- Cannot make any changes

---

## 🔐 How Authentication & Authorization Work

### 1. Authentication (Who are you?)

```
User logs in:
POST /api/v1/auth/login
{
  "email": "admin@weysure.com",
  "password": "weysure123"
}

↓

System checks:
1. Does this email exist? ✓
2. Is the password correct? ✓
3. Is the account active? ✓

↓

Returns JWT token:
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}

Token contains:
{
  "user_id": "910ca03c-3663-45a1-97bf-21eab2463154",
  "email": "admin@weysure.com",
  "role": "TENANT_ADMIN",
  "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",
  "exp": 1234567890  // Expiration timestamp
}
```

### 2. Authorization (What can you do?)

Every API request includes the token:

```
GET /api/v1/knowledge/tenants/bb24bf9e-63c2-4497-b5f5-e46dd916f7ea/sources
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...

↓

System checks:
1. Is token valid? ✓
2. Is user active? ✓
3. Does user have access to this tenant? (verify_tenant_access)
   - Is user super_admin? (bypass all checks) OR
   - Does user.tenant_id == requested tenant_id? ✓
4. Does user's role allow this action?
   - VIEWER can GET but not POST/PATCH/DELETE
   - SUPPORT_AGENT can view and respond to tickets
   - TENANT_ADMIN can do everything for their tenant
   - SUPER_ADMIN can do anything

↓

If all checks pass → Allow request
If any check fails → 403 Forbidden
```

---

## 🔒 Data Isolation Examples

### Example 1: Tenant Admin Trying to Access Another Tenant

```python
# Logged in as: admin@weysure.com (Weysure tenant)
# Their tenant_id: bb24bf9e-63c2-4497-b5f5-e46dd916f7ea

# They try to access AegisScribe's knowledge:
GET /api/v1/knowledge/tenants/999-aegiscribe-tenant-id-999/sources

↓

verify_tenant_access():
  - Is user super_admin? NO
  - Does user.tenant_id (bb24bf9e...) == requested (999...)? NO

→ 403 Forbidden: "Access denied. You don't have permission to access this tenant."
```

### Example 2: Super Admin Can Access Anything

```python
# Logged in as: admin@luranai.com (SUPER_ADMIN)
# Their tenant_id: None
# Their is_superuser: True

# Access Weysure's knowledge:
GET /api/v1/knowledge/tenants/bb24bf9e-63c2-4497-b5f5-e46dd916f7ea/sources

↓

verify_tenant_access():
  - Is user super_admin? YES

→ ✓ Allow request (skip all other checks)

# Access AegisScribe's knowledge:
GET /api/v1/knowledge/tenants/999-aegiscribe-tenant-id-999/sources

↓

verify_tenant_access():
  - Is user super_admin? YES

→ ✓ Allow request
```

### Example 3: Database-Level Isolation

Every query automatically filters by tenant:

```python
# When Weysure admin requests conversations:
db.query(ConversationSession).filter(
    ConversationSession.tenant_id == "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea"
).all()

# Result: Only Weysure's 50 conversations
# AegisScribe's 100 conversations are invisible to this query
```

---

## 🎯 Real-World Scenarios

### Scenario 1: New Company Signs Up

```
1. You (SUPER_ADMIN) create new tenant:
   POST /api/v1/tenants
   {
     "name": "aegiscribe",
     "display_name": "AegisScribe Legal",
     "description": "Legal document automation"
   }

   → New tenant created with UUID

2. You create their admin user:
   POST /api/v1/auth/register
   {
     "email": "admin@aegiscribe.com",
     "password": "secure_password",
     "full_name": "AegisScribe Admin",
     "role": "TENANT_ADMIN",
     "tenant_id": "new-aegis-uuid-here"
   }

3. Their admin logs in:
   - Can only see AegisScribe data
   - Uploads their legal documents
   - Configures their AI settings
   - Cannot see Weysure or any other tenant
```

### Scenario 2: Weysure Hires Support Agent

```
1. Weysure admin (TENANT_ADMIN) logs in:
   admin@weysure.com

2. Creates support agent:
   POST /api/v1/users
   {
     "email": "sarah@weysure.com",
     "password": "agent_password",
     "full_name": "Sarah Johnson",
     "role": "SUPPORT_AGENT",
     "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea"
   }

3. Sarah logs in:
   - Can view Weysure's tickets
   - Can respond to escalated conversations
   - Cannot upload knowledge documents
   - Cannot change AI settings
   - Cannot see AegisScribe data
```

### Scenario 3: Platform-Wide Analytics

```
1. You (SUPER_ADMIN) want to see all usage:
   GET /api/v1/analytics/platform

2. System doesn't filter by tenant_id

3. Returns:
   {
     "total_tenants": 10,
     "total_conversations": 5000,
     "total_tickets": 250,
     "tenants": [
       {
         "name": "weysure",
         "conversations": 500,
         "tickets": 25
       },
       {
         "name": "aegiscribe",
         "conversations": 1000,
         "tickets": 50
       },
       ...
     ]
   }
```

---

## 📊 Visual Summary

### User-Tenant Relationship

```
┌─────────────────────────────────────────────────────────────┐
│                    LURAN PLATFORM                           │
│                 (admin@luranai.com owns)                      │
└─────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ TENANT:       │   │ TENANT:       │   │ TENANT:       │
│ Weysure       │   │ AegisScribe   │   │ EarnHub       │
│               │   │               │   │               │
│ Users:        │   │ Users:        │   │ Users:        │
│ ├─Admin       │   │ ├─Admin       │   │ ├─Admin       │
│ ├─Agent 1     │   │ ├─Agent 1     │   │ ├─Viewer      │
│ └─Agent 2     │   │ └─Agent 2     │   │               │
│               │   │               │   │               │
│ Data:         │   │ Data:         │   │ Data:         │
│ ├─Knowledge   │   │ ├─Knowledge   │   │ ├─Knowledge   │
│ ├─Convos      │   │ ├─Convos      │   │ ├─Convos      │
│ └─Tickets     │   │ └─Tickets     │   │ └─Tickets     │
└───────────────┘   └───────────────┘   └───────────────┘
      ↑                     ↑                   ↑
      └─────────────────────┴───────────────────┘
              SUPER_ADMIN can see all
```

### Permission Matrix

| Action | Super Admin | Tenant Admin | Support Agent | Viewer |
|--------|------------|--------------|---------------|--------|
| View all tenants | ✅ | ❌ | ❌ | ❌ |
| Create tenants | ✅ | ❌ | ❌ | ❌ |
| View own tenant's data | ✅ | ✅ | ✅ | ✅ |
| Upload knowledge | ✅ | ✅ | ❌ | ❌ |
| Configure AI settings | ✅ | ✅ | ❌ | ❌ |
| View conversations | ✅ | ✅ | ✅ | ✅ |
| Respond to tickets | ✅ | ✅ | ✅ | ❌ |
| Create users | ✅ | ✅* | ❌ | ❌ |
| Billing/subscriptions | ✅ | ❌ | ❌ | ❌ |

*Tenant Admin can only create users for their own tenant

---

## 🔑 Key Takeaways

1. **Tenant = Company using your platform**
   - Weysure, AegisScribe, etc.
   - Each has isolated data (knowledge, conversations, tickets)

2. **User = Person managing the platform**
   - NOT the end customer chatting with AI
   - Belongs to one tenant (or none if super admin)

3. **4 Roles with different permissions:**
   - SUPER_ADMIN → Platform owner (you)
   - TENANT_ADMIN → Company administrator
   - SUPPORT_AGENT → Customer support staff
   - VIEWER → Read-only access

4. **Data isolation is automatic:**
   - Every query filters by tenant_id
   - Users can only access their own tenant's data
   - Super admin bypasses all restrictions

5. **tenant_id is the key:**
   - User has tenant_id → Belongs to that company
   - User has tenant_id = NULL → Super admin (platform owner)
   - Data has tenant_id → Belongs to that company

6. **Authentication vs Authorization:**
   - Authentication = Login (who are you?)
   - Authorization = Permissions (what can you do?)

---

## 🚀 When You Launch as SaaS

### Company signs up:

```
1. Signup flow (automated):
   - Company fills form: "AcmeCorp Insurance"
   - System creates tenant: acmecorp
   - System creates admin user: admin@acmecorp.com
   - Sends welcome email with login credentials

2. They log in:
   - Upload their knowledge documents
   - Configure AI brand voice
   - Invite their support team
   - Start chatting with customers

3. Their team structure:
   admin@acmecorp.com → TENANT_ADMIN (full control)
   support1@acmecorp.com → SUPPORT_AGENT (handle tickets)
   support2@acmecorp.com → SUPPORT_AGENT (handle tickets)
   manager@acmecorp.com → VIEWER (see analytics only)

4. You (platform owner):
   - Monitor their usage
   - Charge based on conversations
   - Can access their data for support
   - Can suspend if they don't pay
```

This is how multi-tenant SaaS platforms work! 🎉
