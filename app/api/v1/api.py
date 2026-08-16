from fastapi import APIRouter

from app.api.v1.endpoints import (
    analytics,
    api_keys,
    auditor,
    auth,
    automation,
    automation_sandbox,
    chat,
    company_registration,
    customer_auth,
    customer_conversations,
    customer_engagement,
    customer_explore,
    flows,
    health,
    jobs,
    knowledge,
    leads,
    meetings,
    notifications,
    platform_admin_auth,
    public,
    public_auditor,
    reviews,
    sales,
    subscription,
    tenant,
    tenant_channels,
    tickets,
    users,
    voice,
    webhooks,
    websocket,
    widget,
)
from app.api.v1.endpoints.platform import analytics as platform_analytics
from app.api.v1.endpoints.platform import audit as platform_audit
from app.api.v1.endpoints.platform import emails as platform_emails
from app.api.v1.endpoints.platform import financials as platform_financials
from app.api.v1.endpoints.platform import health as platform_health
from app.api.v1.endpoints.platform import pricing as platform_pricing
from app.api.v1.endpoints.platform import promotional_grants as platform_promotional_grants
from app.api.v1.endpoints.platform import reviews as platform_reviews
from app.api.v1.endpoints.platform import subscriptions as platform_subscriptions
from app.api.v1.endpoints.platform import tenants as platform_tenants
from app.api.v1.endpoints.platform import test_fixtures as platform_test_fixtures
from app.api.v1.endpoints.platform import users as platform_users

api_router = APIRouter()

# Health check endpoint
api_router.include_router(health.router, prefix="/health", tags=["health"])

# Authentication endpoints
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])

# Platform admin authentication endpoints
api_router.include_router(
    platform_admin_auth.router,
    prefix="/platform/auth",
    tags=["platform-admin-auth"],
)

# Company registration endpoints (self-service signup)
api_router.include_router(
    company_registration.router,
    prefix="/auth",
    tags=["registration"],
)

# Customer-facing authentication endpoints
api_router.include_router(
    customer_auth.router,
    prefix="/customer/auth",
    tags=["customer-auth"],
)

# Customer-facing conversation endpoints
api_router.include_router(
    customer_conversations.router,
    prefix="/customer",
    tags=["customer-conversations"],
)

# Customer follow and channels endpoints
api_router.include_router(
    customer_engagement.router,
    prefix="/customer",
    tags=["customer-engagement"],
)

# Customer explore endpoints (personal businesses feed)
api_router.include_router(
    customer_explore.router,
    prefix="/customer/explore",
    tags=["customer-explore"],
)

# User management endpoints (team members)
api_router.include_router(
    users.router,
    prefix="/users",
    tags=["user-management"],
)

# API key management endpoints
api_router.include_router(
    api_keys.router,
    prefix="/api-keys",
    tags=["api-keys"],
)

# Widget configuration endpoints
api_router.include_router(
    widget.router,
    prefix="/widget",
    tags=["widget"],
)

# Public support page and visitor bootstrap endpoints
api_router.include_router(
    public.router,
    prefix="/public",
    tags=["public"],
)

# Public auditor endpoints (no auth, rate-limited)
api_router.include_router(
    public_auditor.router,
    prefix="/public",
    tags=["public-auditor"],
)

# Platform admin tenant management endpoints
api_router.include_router(
    platform_tenants.router,
    prefix="/platform/tenants",
    tags=["platform-tenants"],
)

# Platform admin subscription management endpoints
api_router.include_router(
    platform_subscriptions.router,
    prefix="/platform/subscriptions",
    tags=["platform-subscriptions"],
)

# Platform admin analytics endpoints
api_router.include_router(
    platform_analytics.router,
    prefix="/platform/analytics",
    tags=["platform-analytics"],
)

# Platform admin system health endpoints
api_router.include_router(
    platform_health.router,
    prefix="/platform/health",
    tags=["platform-health"],
)

# Platform admin financial reporting endpoints
api_router.include_router(
    platform_financials.router,
    prefix="/platform/financials",
    tags=["platform-financials"],
)

# Platform admin audit log endpoints
api_router.include_router(
    platform_audit.router,
    prefix="/platform/audit",
    tags=["platform-audit"],
)

# Platform admin review & feedback endpoints
api_router.include_router(
    platform_reviews.router,
    prefix="/platform/reviews",
    tags=["platform-reviews"],
)

# Platform admin user management endpoints
api_router.include_router(
    platform_users.router,
    prefix="/platform/users",
    tags=["platform-users"],
)

# Platform admin email campaign endpoints
api_router.include_router(
    platform_emails.router,
    prefix="/platform/emails",
    tags=["platform-emails"],
)

# Platform admin pricing management endpoints
api_router.include_router(
    platform_pricing.router,
    prefix="/platform/pricing",
    tags=["platform-pricing"],
)

# Platform admin promotional grants endpoints
api_router.include_router(
    platform_promotional_grants.router,
    prefix="/platform/promotional-grants",
    tags=["platform: promotional grants"],
)

# Test-only fixture endpoints (env-gated — 404 outside test/local)
api_router.include_router(
    platform_test_fixtures.router,
    prefix="/platform/test-fixtures",
    tags=["platform: test fixtures"],
)

# Tenant self-management endpoints
api_router.include_router(
    tenant.router,
    prefix="/tenant",
    tags=["tenant"],
)

# Tenant channel publishing endpoints
api_router.include_router(
    tenant_channels.router,
    prefix="/tenant/channels",
    tags=["tenant-channels"],
)

# Tenant automation endpoints
api_router.include_router(
    automation.router,
    prefix="/tenant/automation",
    tags=["tenant-automation"],
)

# Automation sandbox endpoints (for testing/demo action integrations)
api_router.include_router(
    automation_sandbox.router,
    prefix="/automation-sandbox",
    tags=["automation-sandbox"],
)

# Subscription management endpoints
api_router.include_router(
    subscription.router,
    prefix="/subscription",
    tags=["subscription"],
)

# Knowledge management endpoints
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])

# Chat/conversation endpoints
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])

# Voice chat endpoints (ElevenLabs Agents integration)
api_router.include_router(voice.router, prefix="/voice", tags=["voice"])

# Ticket management endpoints
api_router.include_router(tickets.router, prefix="/tickets", tags=["tickets"])

# Flow management endpoints
api_router.include_router(flows.router, prefix="/flows", tags=["flows"])

# Analytics endpoints
api_router.include_router(analytics.router, prefix="/analytics", tags=["analytics"])

# Review and rating endpoints
api_router.include_router(reviews.router, prefix="/reviews", tags=["reviews"])

# Webhook endpoints (Paystack, etc.)
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])

# Job management endpoints (background processing)
api_router.include_router(jobs.router, tags=["jobs"])

# WebSocket endpoints (real-time chat)
api_router.include_router(websocket.router, prefix="/ws", tags=["websocket"])

# Authenticated tenant-dashboard auditor endpoints
api_router.include_router(auditor.router, tags=["auditor"])

# Leads pipeline endpoints
api_router.include_router(leads.router, prefix="/leads", tags=["leads"])

# Sales agent configuration endpoints
api_router.include_router(sales.router, prefix="/sales", tags=["sales"])

# In-app notifications read API (Stage 3 Slice 5c)
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])

# Public widget meeting booking endpoints (Stage 3 Slice 3)
api_router.include_router(meetings.router, prefix="/chat", tags=["meetings"])
