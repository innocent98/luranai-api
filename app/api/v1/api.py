from fastapi import APIRouter

from app.api.v1.endpoints import (
    health,
    auth,
    platform_admin_auth,
    company_registration,
    users,
    api_keys,
    widget,
    tenants,
    knowledge,
    chat,
    tickets,
    jobs,
)

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

# Tenant management endpoints
api_router.include_router(tenants.router, prefix="/tenants", tags=["tenants"])

# Knowledge management endpoints
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])

# Chat/conversation endpoints
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])

# Ticket management endpoints
api_router.include_router(tickets.router, prefix="/tickets", tags=["tickets"])

# Job management endpoints (background processing)
api_router.include_router(jobs.router, tags=["jobs"])
