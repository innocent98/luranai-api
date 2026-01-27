from fastapi import APIRouter

from app.api.v1.endpoints import (
    health,
    auth,
    platform_admin_auth,
    company_registration,
    users,
    api_keys,
    widget,
    tenant,
    subscription,
    knowledge,
    chat,
    tickets,
    jobs,
    websocket,
    analytics,
    reviews,
    webhooks,
)
from app.api.v1.endpoints.platform import tenants as platform_tenants
from app.api.v1.endpoints.platform import subscriptions as platform_subscriptions

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

# Tenant self-management endpoints
api_router.include_router(
    tenant.router,
    prefix="/tenant",
    tags=["tenant"],
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

# Ticket management endpoints
api_router.include_router(tickets.router, prefix="/tickets", tags=["tickets"])

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
