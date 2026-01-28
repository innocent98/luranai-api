# Platform Admin Endpoints - Quick Reference

**Last Updated:** 2026-01-27 ✅ All tested and verified

**Authentication:** All endpoints require Platform Admin token from `POST /platform/auth/login`

---

## 📊 Analytics Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/platform/analytics/dashboard` | Complete dashboard summary | ✅ Required |
| GET | `/platform/analytics/revenue` | MRR, ARR, growth rates | ✅ Required |
| GET | `/platform/analytics/subscriptions` | Churn, conversion, counts | ✅ Required |
| GET | `/platform/analytics/growth?days=30` | Signups, upgrades, trends | ✅ Required |
| GET | `/platform/analytics/usage` | Conversations, tickets, knowledge | ✅ Required |
| GET | `/platform/analytics/cohorts` | Retention analysis | ✅ Required |

---

## 🏥 System Health Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/platform/health/jobs` | Background job status | ✅ Required |
| GET | `/platform/health/api?days=7` | API performance metrics | ✅ Required |
| GET | `/platform/health/database` | Database health | ✅ Required |
| GET | `/platform/health/queue` | Redis queue status | ✅ Required |

---

## 💰 Financial Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/platform/financials/revenue-breakdown` | Revenue by tier/cycle | ✅ Required |
| GET | `/platform/financials/payment-analytics?days=30` | Payment success rates | ✅ Required |
| GET | `/platform/financials/forecast?months=6` | MRR/ARR forecast | ✅ Required |
| GET | `/platform/financials/refunds` | Refund tracking | ✅ Required |
| GET | `/platform/financials/summary` | Complete financial overview | ✅ Required |

---

## 📋 Audit Log Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/platform/audit/logs?limit=5` | Search audit logs | ✅ Required |
| GET | `/platform/audit/security-events?days=7` | Security events | ✅ Required |

---

## ⭐ Review & Feedback Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/platform/reviews/ratings/all?limit=5` | Cross-tenant ratings | ✅ Required |
| GET | `/platform/reviews/feedback/platform?limit=5` | Platform feedback | ✅ Required |
| GET | `/platform/reviews/sentiment-analysis?days=30` | Sentiment analysis | ✅ Required |
| GET | `/platform/reviews/tenant-health-scores?limit=10` | Tenant health scores | ✅ Required |

---

## 👥 User Management Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/platform/users/analytics/role-distribution` | User role statistics | ✅ Required |

---

## 🏢 Tenant Management Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/platform/tenants` | List all tenants | ✅ Required |
| GET | `/platform/tenants?subscription_tier=professional` | Filter by tier | ✅ Required |
| GET | `/platform/tenants?trial_expiring_days=7` | Trials expiring soon | ✅ Required |
| GET | `/platform/tenants/{tenant_id}` | Get tenant details | ✅ Required |
| DELETE | `/platform/tenants/{tenant_id}` | Deactivate tenant | ✅ Required |
| PUT | `/platform/tenants/{tenant_id}/capabilities` | Update capabilities | ✅ Required |

**Query Parameters for GET /platform/tenants:**
- `status_filter`: ACTIVE, INACTIVE, SUSPENDED
- `subscription_status`: trial, active, trial_expired, past_due, canceled
- `subscription_tier`: trial, lite, professional, enterprise
- `trial_expiring_days`: 0-90 (find trials expiring within N days)
- `limit`: 1-1000 (default: 100)
- `offset`: pagination offset

---

## 💳 Subscription Management Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/platform/subscriptions/{tenant_id}/override` | Manual subscription override | ✅ Required |
| POST | `/platform/subscriptions/{tenant_id}/trial/extend` | Extend trial period | ✅ Required |
| GET | `/platform/subscriptions/{tenant_id}` | Subscription details | ✅ Required |
| GET | `/platform/subscriptions/payments/all` | List payment transactions | ✅ Required |

**Query Parameters for GET /platform/subscriptions/payments/all:**
- `tenant_id`: Filter by tenant UUID
- `status_filter`: success, pending, failed
- `limit`: 1-1000 (default: 100)
- `offset`: pagination offset

---

## 🔓 Public Endpoints (No Auth)

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/subscription/plans` | Public pricing (trial, lite, professional) | ❌ Not required |

**Note:** Enterprise tier is excluded from public plans (contact-only pricing)

---

## 👤 User Subscription Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/subscription/my-subscription` | View current subscription | ✅ Required (Tenant User) |
| POST | `/subscription/cancel` | Cancel subscription | ✅ Required (Tenant Admin) |
| GET | `/subscription/history?limit=5` | Subscription history | ✅ Required (Tenant User) |

---

## 🔐 Authentication

### Platform Admin Login
```bash
POST /platform/auth/login
{
  "email": "admin@luranai.com",
  "password": "admin123"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 604800
}
```

### Tenant User Login
```bash
POST /auth/login
{
  "email": "admin@testcompany.com",
  "password": "test123"
}
```

---

## 📝 Example Usage

### Get Platform Analytics Dashboard
```bash
curl -H "Authorization: Bearer {platform_admin_token}" \
  http://localhost:8000/api/v1/platform/analytics/dashboard
```

### List Tenants with Professional Tier
```bash
curl -H "Authorization: Bearer {platform_admin_token}" \
  "http://localhost:8000/api/v1/platform/tenants?subscription_tier=professional&limit=10"
```

### View Payment Transactions
```bash
curl -H "Authorization: Bearer {platform_admin_token}" \
  "http://localhost:8000/api/v1/platform/subscriptions/payments/all?status_filter=success&limit=20"
```

### Manual Subscription Override
```bash
curl -X POST \
  -H "Authorization: Bearer {platform_admin_token}" \
  -H "Content-Type: application/json" \
  -d '{
    "tier": "professional",
    "billing_cycle": "annual",
    "reason": "Enterprise deal",
    "apply_tier_capabilities": true
  }' \
  http://localhost:8000/api/v1/platform/subscriptions/{tenant_id}/override
```

### Extend Trial
```bash
curl -X POST \
  -H "Authorization: Bearer {platform_admin_token}" \
  -H "Content-Type: application/json" \
  -d '{
    "days": 14,
    "reason": "Sales demo extension"
  }' \
  http://localhost:8000/api/v1/platform/subscriptions/{tenant_id}/trial/extend
```

---

## ✅ Verified Status

All endpoints have been tested and verified working on 2026-01-27.

See `FINAL_TEST_RESULTS.md` for detailed test results and sample responses.

---

## 💡 Common Patterns

### Pagination
Most list endpoints support:
- `limit`: Maximum results (1-1000, default varies)
- `offset`: Skip N results

### Filtering
Tenant endpoint supports multiple filters simultaneously:
```bash
GET /platform/tenants?subscription_status=active&subscription_tier=professional&limit=50
```

### Date Ranges
Some endpoints support date filtering:
```bash
GET /platform/analytics/revenue?start_date=2026-01-01&end_date=2026-01-31
```

### Response Format
All endpoints return JSON with consistent error format:
```json
{
  "detail": "Error message here"
}
```

---

## 🎯 Response Status Codes

| Code | Meaning | When |
|------|---------|------|
| 200 | Success | Request completed successfully |
| 400 | Bad Request | Invalid input data |
| 401 | Unauthorized | Missing or invalid authentication token |
| 403 | Forbidden | Insufficient permissions (not SUPER_ADMIN) |
| 404 | Not Found | Resource doesn't exist |
| 422 | Unprocessable Entity | Validation error in request data |
| 500 | Internal Server Error | Server-side error (check logs) |

---

**For detailed API documentation with request/response examples, see:**
- `docs/FRONTEND_API_DOCUMENTATION.md`
- `FINAL_TEST_RESULTS.md`
