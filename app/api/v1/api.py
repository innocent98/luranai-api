from fastapi import APIRouter

from app.api.v1.endpoints import health, auth, tenants, knowledge, chat, tickets, jobs

api_router = APIRouter()

# Health check endpoint
api_router.include_router(health.router, prefix="/health", tags=["health"])

# Authentication endpoints
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])

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
