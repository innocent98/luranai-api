# Rate Limiter Reference

This document provides a comprehensive reference for all HTTP rate limiters in the Luran API.

## Overview

All rate limiters use Redis for distributed rate limiting and are applied at the endpoint level via FastAPI dependencies.

## Rate Limiter Configurations

### Authentication & Registration

#### Regular User Login
- **Endpoint:** `POST /api/v1/auth/login`
- **Rate Limiter:** `auth_rate_limit`
- **Limit:** 5 attempts per 10 minutes
- **Identifier:** IP Address
- **Purpose:** Protect against brute force login attempts while allowing legitimate users to retry
- **Key Prefix:** `http_auth`

#### Platform Admin Login
- **Endpoint:** `POST /api/v1/platform-admin/login`
- **Rate Limiter:** `platform_admin_auth_rate_limit`
- **Limit:** 3 attempts per 15 minutes
- **Identifier:** IP Address
- **Purpose:** Stricter protection for privileged admin accounts
- **Key Prefix:** `http_platform_admin_auth`

#### Company Registration
- **Endpoint:** `POST /api/v1/auth/register-company`
- **Rate Limiter:** `registration_rate_limit`
- **Limit:** 3 requests per hour
- **Identifier:** IP Address
- **Purpose:** Prevent spam account creation
- **Key Prefix:** `http_registration`

#### Email Verification
- **Endpoint:** `POST /api/v1/auth/verify-email`
- **Rate Limiter:** `verification_rate_limit`
- **Limit:** 10 requests per hour
- **Identifier:** IP Address
- **Purpose:** Allow multiple verification attempts without blocking legitimate users
- **Key Prefix:** `http_verification`

#### Resend Verification
- **Endpoint:** `POST /api/v1/auth/resend-verification`
- **Rate Limiter:** `resend_verification_rate_limit`
- **Limit:** 5 requests per hour
- **Identifier:** IP Address
- **Purpose:** Balance between allowing retries and preventing email spam
- **Key Prefix:** `http_resend_verification`

---

### Chat & Communication

#### Chat Messages
- **Endpoints:** `POST /api/v1/chat/*`
- **Rate Limiter:** `chat_rate_limit`
- **Limit:** 20 requests per minute
- **Identifier:** User ID
- **Purpose:** Prevent chat spam while supporting real-time conversation
- **Key Prefix:** `http_chat`

---

### File Operations

#### Knowledge Upload
- **Endpoints:**
  - `POST /api/v1/knowledge-upload/file`
  - `POST /api/v1/knowledge-upload/url`
- **Rate Limiter:** `upload_rate_limit`
- **Limit:** 10 requests per hour
- **Identifier:** User ID
- **Purpose:** Prevent abuse of file processing resources
- **Key Prefix:** `http_upload`

---

### Payment & Subscription

#### Payment Operations
- **Endpoints:**
  - `POST /api/v1/subscription/upgrade/initialize`
  - `POST /api/v1/subscription/upgrade/verify`
  - `POST /api/v1/subscription/upgrade`
- **Rate Limiter:** `payment_rate_limit`
- **Limit:** 5 requests per 5 minutes
- **Identifier:** User ID
- **Purpose:** Prevent payment fraud and duplicate transactions
- **Key Prefix:** `http_payment`

#### Subscription Management
- **Endpoints:**
  - `GET /api/v1/subscription/plans`
  - `GET /api/v1/subscription/my-subscription`
  - `POST /api/v1/subscription/cancel`
  - `GET /api/v1/subscription/history`
- **Rate Limiter:** `subscription_rate_limit`
- **Limit:** 10 requests per minute
- **Identifier:** User ID
- **Purpose:** General subscription operations (less restrictive than payments)
- **Key Prefix:** `http_subscription`

---

### Team Management

#### Team Operations
- **Endpoints:** All `/api/v1/users/*` endpoints
  - `POST /api/v1/users/invite`
  - `GET /api/v1/users`
  - `GET /api/v1/users/{user_id}`
  - `PUT /api/v1/users/{user_id}`
  - `DELETE /api/v1/users/{user_id}`
  - `POST /api/v1/users/{user_id}/reset-password`
- **Rate Limiter:** `team_management_rate_limit`
- **Limit:** 20 requests per minute
- **Identifier:** User ID
- **Purpose:** Allow frequent team management operations
- **Key Prefix:** `http_team_mgmt`

---

### Platform Admin Operations

#### Platform Admin Actions
- **Endpoints:** Most `/api/v1/platform/*` endpoints
  - Tenant management
  - User management
  - Subscription management
  - Reviews and ratings
  - Audit logs
  - Pricing management
- **Rate Limiter:** `platform_admin_rate_limit`
- **Limit:** 60 requests per minute
- **Identifier:** User ID
- **Purpose:** Allow platform admins to perform administrative tasks efficiently
- **Key Prefix:** `http_platform_admin`

#### Platform Analytics
- **Endpoints:** Analytics and financial endpoints
  - `/api/v1/platform/analytics/*`
  - `/api/v1/platform/financials/*`
- **Rate Limiter:** `platform_analytics_rate_limit`
- **Limit:** 100 requests per minute
- **Identifier:** User ID
- **Purpose:** More permissive limit for read-heavy analytics operations
- **Key Prefix:** `http_platform_analytics`

---

### Webhooks

#### Webhook Handlers
- **Endpoints:**
  - `POST /api/v1/webhooks/paystack`
  - `POST /api/v1/webhooks/stripe` (if implemented)
- **Rate Limiter:** `webhook_rate_limit`
- **Limit:** 100 requests per minute
- **Identifier:** IP Address
- **Purpose:** Handle high-volume webhook traffic from payment providers
- **Key Prefix:** `http_webhook`

---

## Rate Limiter Summary Table

| Category | Rate Limiter | Max Calls | Window | Identifier |
|----------|--------------|-----------|---------|------------|
| **Auth - Regular Login** | `auth_rate_limit` | 5 | 10 min | IP |
| **Auth - Admin Login** | `platform_admin_auth_rate_limit` | 3 | 15 min | IP |
| **Auth - Registration** | `registration_rate_limit` | 3 | 1 hour | IP |
| **Auth - Verification** | `verification_rate_limit` | 10 | 1 hour | IP |
| **Auth - Resend** | `resend_verification_rate_limit` | 5 | 1 hour | IP |
| **Chat** | `chat_rate_limit` | 20 | 1 min | User |
| **Upload** | `upload_rate_limit` | 10 | 1 hour | User |
| **Payment** | `payment_rate_limit` | 5 | 5 min | User |
| **Subscription** | `subscription_rate_limit` | 10 | 1 min | User |
| **Team Mgmt** | `team_management_rate_limit` | 20 | 1 min | User |
| **Platform Admin** | `platform_admin_rate_limit` | 60 | 1 min | User |
| **Platform Analytics** | `platform_analytics_rate_limit` | 100 | 1 min | User |
| **Webhooks** | `webhook_rate_limit` | 100 | 1 min | IP |

---

## Implementation Details

### Location
- **Configuration:** `app/utils/http_rate_limiter.py`
- **Redis-based:** Uses `RedisRateLimiter` from `app/utils/rate_limiter.py`
- **Applied via:** FastAPI `Depends()` decorator on routes

### Identifier Types
- **IP-based:** Used for public/unauthenticated endpoints (registration, login, webhooks)
- **User-based:** Used for authenticated endpoints (chat, subscriptions, team management)

### Error Response
When rate limit is exceeded, the API returns:
```json
{
  "detail": {
    "error": "Rate limit exceeded",
    "limit": 5,
    "window_seconds": 600,
    "retry_after_seconds": 600
  }
}
```

**HTTP Status:** `429 Too Many Requests`

**Headers:** `Retry-After: {seconds}`

---

## Modifying Rate Limits

To modify a rate limit:

1. Edit the configuration in `app/utils/http_rate_limiter.py`
2. Update the `max_calls` or `window_seconds` values
3. Restart the API service
4. Update this documentation

Example:
```python
auth_rate_limiter = RedisRateLimiter(
    key_prefix="http_auth",
    max_calls=5,        # Change this
    window_seconds=600, # Or this
)
```

---

## Best Practices

1. **Public endpoints** (registration, login) should be **IP-based** to prevent abuse
2. **Authenticated endpoints** should be **user-based** for better UX
3. **Write operations** (payments, uploads) should have **stricter limits** than reads
4. **Analytics endpoints** can have **more permissive limits** as they're read-only
5. **Admin endpoints** need **stricter login limits** due to elevated privileges
6. **Webhook endpoints** need **high limits** to handle provider traffic bursts

---

## Monitoring

Rate limiter usage is logged via Loguru when limits are exceeded:
```
Rate limit exceeded for {identifier}: {current_calls}/{max_calls}
```

Monitor these logs to identify:
- Potential abuse attempts
- Legitimate users being blocked (limits may be too strict)
- Endpoints that may need limit adjustments

---

**Last Updated:** 2026-01-29
**Version:** 1.0
