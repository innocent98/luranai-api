import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.logger import log
from app.core.websocket import connection_manager
from app.services.customer_presence import presence_tracker
from app.services.whatsapp_service import WhatsAppError
from app.utils.knowledge_failures import KnowledgeError


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time

        log.info(
            f"{request.method} {request.url.path} "
            f"completed in {process_time:.4f}s with status {response.status_code}"
        )

        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # XSS protection
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Permissions policy
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        # CSP (customize for your needs)
        if settings.ENVIRONMENT == "production":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "font-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none';"
            )

        # HSTS (only if HTTPS configured)
        if settings.ENVIRONMENT == "production" and settings.ENABLE_HSTS:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response


_enable_docs = settings.ENVIRONMENT not in ("production", "staging")

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json" if _enable_docs else None,
    docs_url=f"{settings.API_V1_STR}/docs" if _enable_docs else None,
    redoc_url=f"{settings.API_V1_STR}/redoc" if _enable_docs else None,
)

# CORS middleware - environment-based configuration
if settings.ENVIRONMENT == "production":
    allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
    allowed_headers = ["Content-Type", "Authorization", "X-API-Key", "X-Visitor-Id"]
else:
    allowed_methods = ["*"]
    allowed_headers = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=allowed_methods,
    allow_headers=allowed_headers,
    max_age=600,
)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# Logging middleware
app.add_middleware(LoggingMiddleware)


@app.exception_handler(WhatsAppError)
async def whatsapp_error_handler(request: Request, exc: WhatsAppError) -> JSONResponse:
    """WhatsApp endpoints return the contract's flat `{code, message}` error body."""
    return JSONResponse(
        status_code=exc.status_code, content={"code": exc.code, "message": exc.message}
    )


@app.exception_handler(KnowledgeError)
async def knowledge_error_handler(request: Request, exc: KnowledgeError) -> JSONResponse:
    """Knowledge uploads return the flat `{code, message[, min_chars]}` error body."""
    return JSONResponse(status_code=exc.status_code, content=exc.body())


# Include API router
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.on_event("startup")
async def startup_event() -> None:
    await connection_manager.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await connection_manager.stop()
    await presence_tracker.aclose()


@app.get("/")
def root():
    return {
        "message": f"Welcome to {settings.PROJECT_NAME} API",
        "version": settings.VERSION,
        "docs": f"{settings.API_V1_STR}/docs",
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


def start():
    """Entry point for poetry script."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.SERVER_PORT,
        reload=True if settings.ENVIRONMENT == "development" else False,
    )


if __name__ == "__main__":
    start()
