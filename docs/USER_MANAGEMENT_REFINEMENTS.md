# User Management Refinements & Clarifications

## 🎯 Current State Issues

### Problem 1: Platform Admins Mixed with Tenant Users
Currently, both platform admins (Luran staff) and tenant users (company staff) are in the same `users` table:

```sql
-- Current (WRONG):
users table:
├─ admin@luran.dev (SUPER_ADMIN, tenant_id=NULL)      ← Platform admin
└─ admin@weysure.com (TENANT_ADMIN, tenant_id=UUID)   ← Tenant user
```

**Issues:**
- ❌ Platform admins shouldn't have `tenant_id` field (confusing NULL value)
- ❌ Different authentication requirements (2FA for platform admins?)
- ❌ Different permissions structure
- ❌ Mixing concerns (platform vs tenant management)

### Problem 2: Missing User Management Endpoints
- ✅ `/api/v1/auth/register` exists but unclear purpose (who uses it?)
- ❌ `/api/v1/users` MISSING - No way for TENANT_ADMIN to create SUPPORT_AGENT or VIEWER
- ❌ `/api/v1/tenants/{tenant_id}/users` MISSING - No tenant-scoped user management

### Problem 3: Unclear Registration Flow
When a company signs up, what happens?
- Who creates the tenant?
- Who creates the TENANT_ADMIN user?
- Can companies self-register or admin-only?

---

## ✅ Proposed Solution

### 1. Separate Platform Admins Table

Create two distinct tables:

```sql
-- Platform admins (Luran staff)
platform_admins
├─ id (UUID)
├─ email (unique)
├─ hashed_password
├─ full_name
├─ role (ADMIN, SUPER_ADMIN)  ← Only platform roles
├─ is_active
├─ last_login_at
├─ created_at
└─ updated_at

-- Tenant users (Company staff)
users
├─ id (UUID)
├─ email (unique)
├─ hashed_password
├─ full_name
├─ role (TENANT_ADMIN, SUPPORT_AGENT, VIEWER)  ← Only tenant roles
├─ tenant_id (NOT NULL, FK to tenants)  ← Always set!
├─ is_active
├─ last_login_at
├─ created_at
└─ updated_at
```

### Platform Admin Roles

```python
class PlatformAdminRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"  # Full platform control
    ADMIN = "admin"              # Can manage tenants, view data
    # Future: BILLING_ADMIN, SUPPORT_ADMIN, etc.
```

---

## 📋 Registration & User Creation Flows

### Flow 1: Company Self-Registration (Recommended for SaaS)

```
┌─────────────────────────────────────────────────────────┐
│  1. Company visits: https://luran.com/signup            │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  2. Fills form:                                         │
│     - Company Name: "AcmeCorp Insurance"                │
│     - Email: admin@acmecorp.com                         │
│     - Password: ********                                │
│     - Full Name: John Doe                               │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  3. POST /api/v1/auth/register-company                  │
│     {                                                   │
│       "company_name": "AcmeCorp Insurance",             │
│       "company_slug": "acmecorp",                       │
│       "admin_email": "admin@acmecorp.com",              │
│       "admin_password": "********",                     │
│       "admin_full_name": "John Doe"                     │
│     }                                                   │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  4. Backend (in ONE transaction):                       │
│     a) Create tenant:                                   │
│        - name: "acmecorp"                               │
│        - display_name: "AcmeCorp Insurance"             │
│        - status: ACTIVE                                 │
│        - Generate API key                               │
│                                                         │
│     b) Create default AI config for tenant              │
│                                                         │
│     c) Create default capabilities for tenant           │
│                                                         │
│     d) Create TENANT_ADMIN user:                        │
│        - email: admin@acmecorp.com                      │
│        - role: TENANT_ADMIN                             │
│        - tenant_id: <new tenant UUID>                   │
│                                                         │
│     e) Send welcome email                               │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  5. Return:                                             │
│     {                                                   │
│       "tenant_id": "uuid-here",                         │
│       "user": {...},                                    │
│       "access_token": "jwt-here"                        │
│     }                                                   │
│     → Auto-login, redirect to onboarding               │
└─────────────────────────────────────────────────────────┘
```

### Flow 2: Platform Admin Creates Tenant (For Enterprise/Manual)

```
┌─────────────────────────────────────────────────────────┐
│  1. Platform admin logs in: admin@luran.dev             │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  2. POST /api/v1/tenants                                │
│     Authorization: Bearer <platform-admin-jwt>          │
│     {                                                   │
│       "name": "acmecorp",                               │
│       "display_name": "AcmeCorp Insurance",             │
│       "description": "Insurance company"                │
│     }                                                   │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  3. Tenant created                                      │
│     → UUID returned                                     │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  4. POST /api/v1/tenants/{tenant_id}/users              │
│     Authorization: Bearer <platform-admin-jwt>          │
│     {                                                   │
│       "email": "admin@acmecorp.com",                    │
│       "password": "temp-password-123",                  │
│       "full_name": "John Doe",                          │
│       "role": "TENANT_ADMIN"                            │
│     }                                                   │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  5. TENANT_ADMIN user created                           │
│     → Send invite email with password reset link        │
└─────────────────────────────────────────────────────────┘
```

### Flow 3: TENANT_ADMIN Creates Team Members

```
┌─────────────────────────────────────────────────────────┐
│  1. TENANT_ADMIN logs in: admin@acmecorp.com            │
│     (tenant_id: acmecorp-uuid)                          │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  2. POST /api/v1/users                                  │
│     Authorization: Bearer <tenant-admin-jwt>            │
│     {                                                   │
│       "email": "sarah@acmecorp.com",                    │
│       "password": "temp-pass-456",                      │
│       "full_name": "Sarah Johnson",                     │
│       "role": "SUPPORT_AGENT"                           │
│     }                                                   │
│     NOTE: tenant_id comes from JWT, not request body!   │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  3. Backend validates:                                  │
│     - Is requester TENANT_ADMIN? ✓                      │
│     - Extract tenant_id from JWT                        │
│     - Role allowed? (SUPPORT_AGENT or VIEWER only)      │
│     - Cannot create TENANT_ADMIN (prevent privilege)    │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  4. Create user:                                        │
│     - email: sarah@acmecorp.com                         │
│     - role: SUPPORT_AGENT                               │
│     - tenant_id: acmecorp-uuid (from JWT)               │
│     → Send invite email                                 │
└─────────────────────────────────────────────────────────┘
```

---

## 🛠️ Required API Endpoints

### A. Platform Admin Endpoints (New)

```yaml
# Platform admin login
POST /api/v1/platform/auth/login
Body:
  email: admin@luran.dev
  password: ********
Response:
  access_token: jwt-token
  token_type: bearer

# List all tenants (platform admin only)
GET /api/v1/platform/tenants
Auth: Platform Admin JWT
Response:
  - id: uuid
    name: weysure
    display_name: Weysure Insurance
    status: ACTIVE
    created_at: ...

# Create tenant (platform admin only)
POST /api/v1/platform/tenants
Auth: Platform Admin JWT
Body:
  name: acmecorp
  display_name: AcmeCorp Insurance
  description: Insurance company

# Create tenant admin user (platform admin only)
POST /api/v1/platform/tenants/{tenant_id}/users
Auth: Platform Admin JWT
Body:
  email: admin@acmecorp.com
  password: temp-password
  full_name: John Doe
  role: TENANT_ADMIN
```

### B. Public Registration Endpoint (New)

```yaml
# Company self-registration
POST /api/v1/auth/register-company
Body:
  company_name: AcmeCorp Insurance
  company_slug: acmecorp
  admin_email: admin@acmecorp.com
  admin_password: ********
  admin_full_name: John Doe
Response:
  tenant_id: uuid
  user:
    id: uuid
    email: admin@acmecorp.com
    role: TENANT_ADMIN
  access_token: jwt-token
```

### C. Tenant User Management Endpoints (New)

```yaml
# Create team member (TENANT_ADMIN only)
POST /api/v1/users
Auth: Tenant Admin JWT
Body:
  email: sarah@acmecorp.com
  password: temp-password  # Or auto-generate and email
  full_name: Sarah Johnson
  role: SUPPORT_AGENT  # or VIEWER
Response:
  id: uuid
  email: sarah@acmecorp.com
  role: SUPPORT_AGENT
  tenant_id: acmecorp-uuid
  is_active: true

# List team members (TENANT_ADMIN only)
GET /api/v1/users
Auth: Tenant Admin JWT
Response:
  - id: uuid
    email: admin@acmecorp.com
    full_name: John Doe
    role: TENANT_ADMIN
    is_active: true
  - id: uuid
    email: sarah@acmecorp.com
    full_name: Sarah Johnson
    role: SUPPORT_AGENT
    is_active: true

# Get user by ID (TENANT_ADMIN only)
GET /api/v1/users/{user_id}
Auth: Tenant Admin JWT
Response:
  id: uuid
  email: sarah@acmecorp.com
  full_name: Sarah Johnson
  role: SUPPORT_AGENT
  tenant_id: acmecorp-uuid

# Update user (TENANT_ADMIN only)
PATCH /api/v1/users/{user_id}
Auth: Tenant Admin JWT
Body:
  full_name: Sarah Smith  # Optional
  role: VIEWER  # Optional (cannot change to TENANT_ADMIN)
  is_active: false  # Optional (deactivate user)

# Delete user (TENANT_ADMIN only)
DELETE /api/v1/users/{user_id}
Auth: Tenant Admin JWT
Response:
  message: User deleted successfully
```

### D. Current Auth Endpoints (Keep, but clarify)

```yaml
# Tenant user login
POST /api/v1/auth/login
Body:
  email: admin@acmecorp.com
  password: ********
Response:
  access_token: jwt-token

# Get current user
GET /api/v1/auth/me
Auth: Tenant User JWT
Response:
  id: uuid
  email: admin@acmecorp.com
  role: TENANT_ADMIN
  tenant_id: acmecorp-uuid

# DEPRECATE: /api/v1/auth/register
# Replace with /api/v1/auth/register-company
```

---

## 🔒 Permission Rules

### Who Can Create What?

| Creator Role | Can Create | Endpoint | Notes |
|-------------|-----------|----------|-------|
| **SUPER_ADMIN** (Platform) | Tenants | `POST /api/v1/platform/tenants` | - |
| **SUPER_ADMIN** (Platform) | Any user for any tenant | `POST /api/v1/platform/tenants/{id}/users` | - |
| **TENANT_ADMIN** | SUPPORT_AGENT, VIEWER | `POST /api/v1/users` | Cannot create TENANT_ADMIN |
| **SUPPORT_AGENT** | Nothing | - | Read-only for users |
| **VIEWER** | Nothing | - | Read-only |
| **Anonymous** | Company + TENANT_ADMIN | `POST /api/v1/auth/register-company` | Self-service signup |

### Validation Rules

```python
# When TENANT_ADMIN creates user:
def create_user_as_tenant_admin(creator: User, new_user_data: UserCreate):
    # 1. Verify creator is TENANT_ADMIN
    if creator.role != UserRole.TENANT_ADMIN:
        raise Forbidden("Only TENANT_ADMIN can create users")

    # 2. Verify role is allowed
    allowed_roles = [UserRole.SUPPORT_AGENT, UserRole.VIEWER]
    if new_user_data.role not in allowed_roles:
        raise Forbidden("Cannot create TENANT_ADMIN users")

    # 3. Force tenant_id from creator's JWT (security!)
    new_user_data.tenant_id = creator.tenant_id

    # 4. Create user
    return create_user(new_user_data)
```

---

## 📊 Database Migration Plan

### Step 1: Create platform_admins table

```sql
CREATE TABLE platform_admins (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL CHECK (role IN ('SUPER_ADMIN', 'ADMIN')),
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_login_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_platform_admins_email ON platform_admins(email);
CREATE INDEX ix_platform_admins_role ON platform_admins(role);
```

### Step 2: Migrate existing super admin

```sql
-- Move admin@luran.dev from users to platform_admins
INSERT INTO platform_admins (id, email, hashed_password, full_name, role, is_active, created_at, updated_at)
SELECT id, email, hashed_password, full_name, 'SUPER_ADMIN', is_active, created_at, updated_at
FROM users
WHERE role = 'SUPER_ADMIN' AND tenant_id IS NULL;

-- Delete from users
DELETE FROM users WHERE role = 'SUPER_ADMIN' AND tenant_id IS NULL;
```

### Step 3: Update users table

```sql
-- Make tenant_id NOT NULL (all tenant users must have tenant)
ALTER TABLE users ALTER COLUMN tenant_id SET NOT NULL;

-- Remove SUPER_ADMIN from user role enum
-- (This requires recreating the enum - complex, can be done later)

-- Add constraint: Users can only have tenant roles
ALTER TABLE users ADD CONSTRAINT check_tenant_user_roles
CHECK (role IN ('TENANT_ADMIN', 'SUPPORT_AGENT', 'VIEWER'));
```

---

## 🚀 Implementation Order

### Phase 1: Database & Models
1. Create `platform_admins` table migration
2. Create `PlatformAdmin` SQLAlchemy model
3. Migrate existing super admin data
4. Update `User` model to enforce tenant_id NOT NULL

### Phase 2: Platform Admin Endpoints
1. Create `/api/v1/platform/auth/login` endpoint
2. Create `/api/v1/platform/tenants` CRUD endpoints
3. Create `/api/v1/platform/tenants/{id}/users` endpoint
4. Add platform admin JWT authentication

### Phase 3: Company Registration
1. Create `/api/v1/auth/register-company` endpoint
2. Implement atomic tenant + user creation
3. Add email verification (optional)
4. Add welcome email flow

### Phase 4: Tenant User Management
1. Create `/api/v1/users` POST endpoint (create)
2. Create `/api/v1/users` GET endpoint (list)
3. Create `/api/v1/users/{id}` GET endpoint (get)
4. Create `/api/v1/users/{id}` PATCH endpoint (update)
5. Create `/api/v1/users/{id}` DELETE endpoint (delete)
6. Add permission checks (TENANT_ADMIN only)

### Phase 5: Frontend & Documentation
1. Update API docs with new endpoints
2. Create admin dashboard for platform admins
3. Create company signup flow
4. Create team management UI for tenant admins
5. Update USER_ROLES_AND_TENANTS_EXPLAINED.md

---

## ✅ Summary of Changes

### Current State (Before)
```
users table:
├─ admin@luran.dev (SUPER_ADMIN, tenant_id=NULL)  ← Mixed concerns
└─ admin@weysure.com (TENANT_ADMIN, tenant_id=UUID)

Endpoints:
- /api/v1/auth/register (unclear purpose)
- Missing: User management endpoints
```

### New State (After)
```
platform_admins table:
└─ admin@luran.dev (SUPER_ADMIN)  ← Platform staff only

users table:
└─ admin@weysure.com (TENANT_ADMIN, tenant_id=UUID)  ← Tenant users only
    └─ sarah@weysure.com (SUPPORT_AGENT, tenant_id=UUID)

Endpoints:
- /api/v1/platform/auth/login (platform admin login)
- /api/v1/platform/tenants (manage tenants)
- /api/v1/auth/register-company (company self-signup)
- /api/v1/users (tenant user management)
```

---

## 🎯 Next Steps

1. **Review this document** - Confirm the approach
2. **Database migration** - Create platform_admins table
3. **Implement endpoints** - Start with platform admin endpoints
4. **Test flows** - Test all 3 registration flows
5. **Update docs** - Reflect new structure in documentation

Questions to confirm:
1. Do you want self-service company registration? (Recommended: YES for SaaS)
2. Should platform admins have 2FA? (Recommended: YES for security)
3. Should we auto-generate passwords for new users and email them? (Recommended: YES)
4. Do you want email verification for new companies? (Recommended: YES for production)
