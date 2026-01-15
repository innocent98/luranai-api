# Frontend Development Guide - Complete API Reference

**Last Updated:** 2026-01-15
**Base URL:** `http://localhost:8000/api/v1` (development) | `https://api.luran.com/api/v1` (production)

## Table of Contents

1. [Authentication Overview](#authentication-overview)
2. [Platform Admin Dashboard](#platform-admin-dashboard)
3. [Company/Tenant Dashboard](#companytenant-dashboard)
4. [Widget Integration](#widget-integration)
5. [Common Patterns](#common-patterns)
6. [Error Handling](#error-handling)

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

## 7. User Flows with Diagrams

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

## 8. Common Patterns

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

## 9. Frontend Pages Checklist

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
- [ ] `/dashboard/settings/password` - Change password

---

## 10. Next Steps

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

## 11. Important Notes

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

---

**Questions?** Contact the backend team for clarification on any endpoints.
