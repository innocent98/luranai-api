# User Management Implementation Plan

## 🎯 Confirmed Requirements

1. ✅ **Self-service company registration** - Companies can sign up themselves
2. ✅ **Auto-generate passwords** - Email temporary password, users can reset
3. ✅ **Email service** - Properly configured with SMTP
4. ✅ **Separate platform admins** - Platform staff in separate table

---

## 📋 Existing Auth Endpoints - Refined Purpose

### Current Endpoints Analysis

```yaml
# 1. POST /api/v1/auth/register
Current: Unclear - creates users but doesn't specify if platform/tenant
REFINE TO: Remove or deprecate
REPLACE WITH: /api/v1/auth/register-company (for companies)
              /api/v1/platform/auth/register (for platform admins)

# 2. POST /api/v1/auth/login
Current: Generic login for all users
REFINE TO: KEEP - But becomes tenant user login only
           Platform admins use different endpoint: /api/v1/platform/auth/login

# 3. GET /api/v1/auth/me
Current: Get current user info
REFINE TO: KEEP - Works for tenant users
           Platform admins get similar endpoint: /api/v1/platform/auth/me

# 4. PUT /api/v1/auth/me/password
Current: Update password
REFINE TO: KEEP - Users change their own password (works for both tenant users & platform admins)
```

### Refined Auth Endpoints Map

```
┌─────────────────────────────────────────────────────────────┐
│                   TENANT USER AUTH                          │
├─────────────────────────────────────────────────────────────┤
│ POST /api/v1/auth/login                                     │
│ └─ Purpose: Tenant user (TENANT_ADMIN, SUPPORT_AGENT,      │
│            VIEWER) logs in                                  │
│ └─ Returns: JWT with tenant_id in payload                   │
│                                                             │
│ GET /api/v1/auth/me                                         │
│ └─ Purpose: Get current tenant user info                    │
│ └─ Returns: User details including tenant_id                │
│                                                             │
│ PUT /api/v1/auth/me/password                                │
│ └─ Purpose: User changes their own password                 │
│ └─ Requires: current_password + new_password                │
│                                                             │
│ POST /api/v1/auth/forgot-password (NEW)                    │
│ └─ Purpose: Request password reset email                    │
│                                                             │
│ POST /api/v1/auth/reset-password (NEW)                     │
│ └─ Purpose: Reset password with token from email            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                  PLATFORM ADMIN AUTH                        │
├─────────────────────────────────────────────────────────────┤
│ POST /api/v1/platform/auth/login (NEW)                     │
│ └─ Purpose: Platform admin (ADMIN, SUPER_ADMIN) logs in     │
│ └─ Returns: JWT with is_platform_admin=true                 │
│                                                             │
│ GET /api/v1/platform/auth/me (NEW)                         │
│ └─ Purpose: Get current platform admin info                 │
│ └─ Returns: Platform admin details (no tenant_id)           │
│                                                             │
│ PUT /api/v1/platform/auth/me/password (NEW)                │
│ └─ Purpose: Platform admin changes password                 │
│ └─ Same as tenant user endpoint                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                 PUBLIC REGISTRATION                         │
├─────────────────────────────────────────────────────────────┤
│ POST /api/v1/auth/register-company (NEW)                   │
│ └─ Purpose: Company self-registration                       │
│ └─ Creates: Tenant + First TENANT_ADMIN user                │
│ └─ Returns: JWT (auto-login)                                │
│ └─ No auth required (public endpoint)                       │
│                                                             │
│ DELETE: POST /api/v1/auth/register                         │
│ └─ Purpose: DEPRECATE - Unclear, replaced by above          │
└─────────────────────────────────────────────────────────────┘
```

---

## 🗂️ Implementation Tasks

### Phase 1: Database & Models (Day 1)

#### Task 1.1: Create platform_admins table migration
```bash
File: alembic/versions/XXXX_create_platform_admins.py

Changes:
- Create platform_admins table
- Create indexes on email, role
- Add updated_at trigger
```

#### Task 1.2: Migrate existing super admin
```bash
File: alembic/versions/XXXX_migrate_super_admin.py

Changes:
- Copy admin@luran.dev from users to platform_admins
- Delete from users table
- Verify migration successful
```

#### Task 1.3: Update users table constraints
```bash
File: alembic/versions/XXXX_update_users_constraints.py

Changes:
- Make tenant_id NOT NULL (all tenant users must belong to tenant)
- Add check constraint: role IN (TENANT_ADMIN, SUPPORT_AGENT, VIEWER)
```

#### Task 1.4: Create PlatformAdmin model
```python
File: app/db/models/platform_admin.py

class PlatformAdminRole(enum.Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"

class PlatformAdmin(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "platform_admins"

    email: Mapped[str]
    hashed_password: Mapped[str]
    full_name: Mapped[str]
    role: Mapped[PlatformAdminRole]
    is_active: Mapped[bool]
    last_login_at: Mapped[Optional[datetime]]
```

#### Task 1.5: Update User model
```python
File: app/db/models/user.py

Changes:
- Remove SUPER_ADMIN from UserRole enum
- Update comments to clarify tenant users only
```

---

### Phase 2: Email Service Setup (Day 1)

#### Task 2.1: Create email service
```python
File: app/services/email_service.py

class EmailService:
    def send_welcome_email(tenant_name, admin_email, temp_password)
    def send_user_invite_email(user_email, temp_password, tenant_name, invited_by)
    def send_password_reset_email(email, reset_token)
    def send_password_changed_notification(email)

Functions:
- Use FastAPI-Mail or similar
- HTML templates for emails
- Queue emails (Redis/Celery) for reliability
```

#### Task 2.2: Create email templates
```html
Files: app/templates/emails/
- welcome.html (company registration)
- user_invite.html (new team member)
- password_reset.html (forgot password)
- password_changed.html (notification)
```

#### Task 2.3: Update config for email
```python
File: app/core/config.py

Ensure SMTP settings are used:
- SMTP_HOST
- SMTP_PORT
- SMTP_USER
- SMTP_PASSWORD
- SMTP_TLS
- EMAILS_FROM_EMAIL
- EMAILS_FROM_NAME
```

---

### Phase 3: Platform Admin Endpoints (Day 2)

#### Task 3.1: Platform admin authentication
```python
File: app/services/platform_admin_auth_service.py

Functions:
- login(email, password) -> Token
- verify_token(token) -> PlatformAdmin
- create_platform_admin(data) -> PlatformAdmin (SUPER_ADMIN only)
```

#### Task 3.2: Platform admin auth endpoints
```python
File: app/api/v1/endpoints/platform/auth.py

Endpoints:
- POST /api/v1/platform/auth/login
- GET /api/v1/platform/auth/me
- PUT /api/v1/platform/auth/me/password
```

#### Task 3.3: Platform admin dependencies
```python
File: app/api/deps.py

New dependencies:
- get_current_platform_admin()
- get_current_super_admin()
- verify_platform_admin()
```

#### Task 3.4: Platform tenant management
```python
File: app/api/v1/endpoints/platform/tenants.py

Endpoints:
- GET /api/v1/platform/tenants (list all tenants)
- POST /api/v1/platform/tenants (create tenant)
- GET /api/v1/platform/tenants/{id} (get tenant details)
- PATCH /api/v1/platform/tenants/{id} (update tenant)
- DELETE /api/v1/platform/tenants/{id} (soft delete/suspend)
```

---

### Phase 4: Company Registration (Day 2-3)

#### Task 4.1: Company registration service
```python
File: app/services/company_registration_service.py

class CompanyRegistrationService:
    async def register_company(data: CompanyRegistrationRequest):
        """
        Atomically creates:
        1. Tenant
        2. Default AI config
        3. Default capabilities
        4. TENANT_ADMIN user
        5. Send welcome email

        Returns: tenant, user, access_token
        """
```

#### Task 4.2: Company registration schemas
```python
File: app/schemas/company.py

class CompanyRegistrationRequest(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=100)
    company_slug: str = Field(..., min_length=2, max_length=50, pattern="^[a-z0-9-]+$")
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=8)
    admin_full_name: str = Field(..., min_length=2, max_length=255)

class CompanyRegistrationResponse(BaseModel):
    tenant: TenantResponse
    user: UserResponse
    access_token: str
    token_type: str = "bearer"
```

#### Task 4.3: Company registration endpoint
```python
File: app/api/v1/endpoints/auth.py

Endpoint:
- POST /api/v1/auth/register-company
  - Public (no auth)
  - Validates company_slug uniqueness
  - Creates tenant + user atomically
  - Sends welcome email
  - Returns JWT for auto-login
```

#### Task 4.4: Deprecate old register endpoint
```python
File: app/api/v1/endpoints/auth.py

Changes:
- Mark POST /api/v1/auth/register as deprecated in OpenAPI docs
- Add note: "Use /api/v1/auth/register-company instead"
- Keep for backward compatibility initially
```

---

### Phase 5: User Management Endpoints (Day 3-4)

#### Task 5.1: User management service
```python
File: app/services/user_management_service.py

class UserManagementService:
    def create_team_member(creator: User, data: UserCreate):
        """
        TENANT_ADMIN creates SUPPORT_AGENT or VIEWER
        - Validate creator is TENANT_ADMIN
        - Validate role (cannot create TENANT_ADMIN)
        - Force tenant_id from creator
        - Generate temp password
        - Send invite email
        """

    def list_team_members(tenant_id: UUID):
        """List all users in tenant"""

    def get_user(user_id: UUID, requester: User):
        """Get user (validate same tenant)"""

    def update_user(user_id: UUID, updates: UserUpdate, requester: User):
        """Update user (validate permissions)"""

    def delete_user(user_id: UUID, requester: User):
        """Soft delete user (is_active=false)"""

    def regenerate_password(user_id: UUID, requester: User):
        """TENANT_ADMIN regenerates password for team member"""
```

#### Task 5.2: User management endpoints
```python
File: app/api/v1/endpoints/users.py

Endpoints:
- POST /api/v1/users (create team member)
  - Auth: TENANT_ADMIN
  - Body: email, full_name, role (SUPPORT_AGENT or VIEWER)
  - Auto-generates password, sends email

- GET /api/v1/users (list team members)
  - Auth: TENANT_ADMIN
  - Returns: All users in requester's tenant

- GET /api/v1/users/{user_id} (get user)
  - Auth: TENANT_ADMIN
  - Validates: user belongs to same tenant

- PATCH /api/v1/users/{user_id} (update user)
  - Auth: TENANT_ADMIN
  - Allowed: full_name, role, is_active
  - Cannot: Change to TENANT_ADMIN

- DELETE /api/v1/users/{user_id} (delete user)
  - Auth: TENANT_ADMIN
  - Soft delete: Set is_active=false

- POST /api/v1/users/{user_id}/regenerate-password (NEW)
  - Auth: TENANT_ADMIN
  - Generates new temp password
  - Sends email to user
```

---

### Phase 6: Password Management (Day 4)

#### Task 6.1: Password reset service
```python
File: app/services/password_reset_service.py

class PasswordResetService:
    def request_reset(email: str):
        """
        - Generate reset token (JWT or UUID)
        - Store in Redis with expiration (15 minutes)
        - Send email with reset link
        """

    def verify_reset_token(token: str) -> User:
        """Verify token is valid and not expired"""

    def reset_password(token: str, new_password: str):
        """Reset password with token"""
```

#### Task 6.2: Password reset endpoints
```python
File: app/api/v1/endpoints/auth.py

New endpoints:
- POST /api/v1/auth/forgot-password
  - Public
  - Body: email
  - Sends reset email

- POST /api/v1/auth/reset-password
  - Public
  - Body: token, new_password
  - Resets password

- PUT /api/v1/auth/me/password (EXISTING - keep)
  - Auth required
  - Body: current_password, new_password
  - User changes own password
```

---

### Phase 7: Router Configuration (Day 4)

#### Task 7.1: Update main router
```python
File: app/api/v1/router.py

Add new routers:
- /platform/auth → platform admin auth
- /platform/tenants → platform tenant management
- /users → tenant user management
```

#### Task 7.2: Update OpenAPI tags
```python
File: app/main.py

New tags:
- "Platform Auth" - Platform admin authentication
- "Platform Tenants" - Platform tenant management
- "User Management" - Tenant user management
- "Auth" - Tenant user authentication
```

---

### Phase 8: Testing & Documentation (Day 5)

#### Task 8.1: Write tests
```python
Files: tests/

Test coverage:
- test_company_registration.py
  - Test successful registration
  - Test duplicate company_slug
  - Test duplicate admin email
  - Test email sent

- test_user_management.py
  - Test TENANT_ADMIN creates SUPPORT_AGENT
  - Test TENANT_ADMIN cannot create TENANT_ADMIN
  - Test SUPPORT_AGENT cannot create users
  - Test cross-tenant access blocked

- test_platform_admin.py
  - Test platform admin login
  - Test platform admin can access all tenants
  - Test tenant user cannot access platform endpoints

- test_password_management.py
  - Test password reset flow
  - Test password change
  - Test password regeneration
```

#### Task 8.2: Update documentation
```markdown
Files: docs/

Updates:
- API_REFERENCE.md (new endpoints)
- USER_ROLES_AND_TENANTS_EXPLAINED.md (reflect new structure)
- GETTING_STARTED.md (company registration flow)
- EMAIL_CONFIGURATION.md (new - email setup guide)
```

---

## 📝 Database Schema After Implementation

```sql
-- Platform admins (Luran staff)
platform_admins
├─ id (UUID, PK)
├─ email (unique)
├─ hashed_password
├─ full_name
├─ role (ADMIN, SUPER_ADMIN)
├─ is_active
├─ last_login_at
├─ created_at
└─ updated_at

-- Tenants (Companies)
tenants
├─ id (UUID, PK)
├─ name (unique) - e.g., "acmecorp"
├─ display_name - e.g., "AcmeCorp Insurance"
├─ description
├─ status (ACTIVE, INACTIVE, SUSPENDED)
├─ api_key_hash
├─ created_at
└─ updated_at

-- Tenant users (Company staff)
users
├─ id (UUID, PK)
├─ email (unique)
├─ hashed_password
├─ full_name
├─ role (TENANT_ADMIN, SUPPORT_AGENT, VIEWER)
├─ tenant_id (NOT NULL, FK to tenants) ← Always set!
├─ is_active
├─ last_login_at
├─ created_at
└─ updated_at

-- Password reset tokens (Redis)
password_reset_tokens
├─ token (key)
├─ user_id (value)
└─ expiration (15 minutes)
```

---

## 🔐 Security Considerations

### 1. JWT Token Structure

**Tenant User JWT:**
```json
{
  "user_id": "uuid",
  "email": "admin@acmecorp.com",
  "role": "TENANT_ADMIN",
  "tenant_id": "tenant-uuid",
  "type": "tenant_user",
  "exp": 1234567890
}
```

**Platform Admin JWT:**
```json
{
  "admin_id": "uuid",
  "email": "admin@luran.dev",
  "role": "SUPER_ADMIN",
  "type": "platform_admin",
  "exp": 1234567890
}
```

### 2. Permission Validation

```python
# Middleware checks token type
def verify_tenant_user_token(token):
    if token.type != "tenant_user":
        raise Forbidden("Platform admin tokens not allowed here")

def verify_platform_admin_token(token):
    if token.type != "platform_admin":
        raise Forbidden("Tenant user tokens not allowed here")
```

### 3. Cross-Tenant Protection

```python
# Every tenant user query filters by tenant_id
def get_users(requester: User):
    return db.query(User).filter(
        User.tenant_id == requester.tenant_id
    ).all()
```

---

## 📧 Email Templates to Create

### 1. Welcome Email (Company Registration)
```
Subject: Welcome to Luran! 🎉

Hi {{admin_name}},

Welcome to Luran! Your account has been created successfully.

Company: {{company_name}}
Login Email: {{admin_email}}
Temporary Password: {{temp_password}}

Get started: https://app.luran.com/login

Please change your password after logging in.

Questions? Reply to this email.

Best,
Luran Team
```

### 2. Team Member Invite
```
Subject: You've been invited to {{company_name}} on Luran

Hi {{user_name}},

{{invited_by}} has invited you to join {{company_name}} on Luran as a {{role}}.

Login Email: {{user_email}}
Temporary Password: {{temp_password}}

Get started: https://app.luran.com/login

Please change your password after logging in.

Questions? Contact {{invited_by_email}}.

Best,
Luran Team
```

### 3. Password Reset
```
Subject: Reset your Luran password

Hi {{user_name}},

We received a request to reset your password.

Reset your password: {{reset_link}}

This link expires in 15 minutes.

If you didn't request this, ignore this email.

Best,
Luran Team
```

### 4. Password Changed Notification
```
Subject: Your password was changed

Hi {{user_name}},

Your password was successfully changed.

If you didn't make this change, contact us immediately.

Best,
Luran Team
```

---

## 🚀 Implementation Timeline

| Day | Phase | Tasks | Deliverables |
|-----|-------|-------|--------------|
| 1 | Database & Email | 1.1-1.5, 2.1-2.3 | ✅ platform_admins table<br>✅ Email service working |
| 2 | Platform Admin | 3.1-3.4 | ✅ Platform admin can login<br>✅ Can create tenants |
| 2-3 | Company Reg | 4.1-4.4 | ✅ Companies can self-register<br>✅ Welcome email sent |
| 3-4 | User Management | 5.1-5.2 | ✅ Tenant admin can create users<br>✅ Invite emails sent |
| 4 | Password Mgmt | 6.1-6.2, 7.1-7.2 | ✅ Password reset working<br>✅ All routers configured |
| 5 | Testing & Docs | 8.1-8.2 | ✅ Tests passing<br>✅ Docs updated |

---

## ✅ Acceptance Criteria

### Company Registration Flow
- [ ] Company can sign up at /signup
- [ ] Tenant + TENANT_ADMIN created atomically
- [ ] Welcome email sent with temp password
- [ ] Auto-login after registration
- [ ] Email verification (optional)

### User Management Flow
- [ ] TENANT_ADMIN can create SUPPORT_AGENT
- [ ] TENANT_ADMIN can create VIEWER
- [ ] TENANT_ADMIN CANNOT create TENANT_ADMIN
- [ ] Invite email sent with temp password
- [ ] TENANT_ADMIN can list all team members
- [ ] TENANT_ADMIN can deactivate users
- [ ] TENANT_ADMIN can regenerate passwords

### Platform Admin Flow
- [ ] Platform admin can login
- [ ] Platform admin can see all tenants
- [ ] Platform admin can create tenants
- [ ] Platform admin can create TENANT_ADMIN for any tenant
- [ ] Tenant user CANNOT access platform endpoints

### Password Management
- [ ] User can change own password (current + new)
- [ ] User can request password reset
- [ ] Reset email sent with link
- [ ] Password reset works with token
- [ ] TENANT_ADMIN can regenerate user password

### Email Service
- [ ] SMTP configured correctly
- [ ] Welcome emails sent
- [ ] Invite emails sent
- [ ] Password reset emails sent
- [ ] Password changed notifications sent
- [ ] HTML templates working
- [ ] Unsubscribe links (optional)

---

## 🔄 Next Steps

1. **Review this plan** - Confirm approach
2. **Provide email credentials** - SMTP details
3. **Start Phase 1** - Database migrations
4. **Test each phase** - Before moving to next
5. **Deploy** - Once all tests pass

Ready to start? I'll begin with Phase 1 (Database & Models) once you confirm!
