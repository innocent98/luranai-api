"""HTTP endpoint rate limiting using Redis."""

import time
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.logger import log
from app.utils.rate_limiter import RedisRateLimiter

# Rate limiters for different endpoint types
# Staging and development need higher limits to accommodate E2E test runs.
_is_relaxed = settings.ENVIRONMENT in ("staging", "development")

# Regular user login - moderate protection
auth_rate_limiter = RedisRateLimiter(
    key_prefix="http_auth",
    max_calls=50 if _is_relaxed else 5,
    window_seconds=600,  # 5 per 10 minutes in prod (brute force protection)
)

# Platform admin login - stricter protection (more sensitive endpoint)
platform_admin_auth_rate_limiter = RedisRateLimiter(
    key_prefix="http_platform_admin_auth",
    max_calls=30 if _is_relaxed else 3,
    window_seconds=900,  # 3 per 15 minutes in prod (stricter for admin access)
)

chat_rate_limiter = RedisRateLimiter(
    key_prefix="http_chat",
    max_calls=20,
    window_seconds=60,
)

upload_rate_limiter = RedisRateLimiter(
    key_prefix="http_upload",
    max_calls=10,
    window_seconds=3600,
)

# Public registration endpoints (IP-based, separate limits)
registration_rate_limiter = RedisRateLimiter(
    key_prefix="http_registration",
    max_calls=3,
    window_seconds=3600,  # 3 registrations per hour (prevent spam)
)

verification_rate_limiter = RedisRateLimiter(
    key_prefix="http_verification",
    max_calls=10,
    window_seconds=3600,  # 10 verifications per hour (user already registered)
)

resend_verification_rate_limiter = RedisRateLimiter(
    key_prefix="http_resend_verification",
    max_calls=5,
    window_seconds=3600,  # 5 resends per hour (allow retries, prevent spam)
)

# Webhook endpoints (IP-based)
webhook_rate_limiter = RedisRateLimiter(
    key_prefix="http_webhook",
    max_calls=100,
    window_seconds=60,
)

# Payment operations (user-based)
payment_rate_limiter = RedisRateLimiter(
    key_prefix="http_payment",
    max_calls=5,
    window_seconds=300,  # 5 per 5 minutes
)

# Subscription operations (user-based)
subscription_rate_limiter = RedisRateLimiter(
    key_prefix="http_subscription",
    max_calls=10,
    window_seconds=60,
)

# Subscription reads (user-based, more permissive for dashboard navigation)
subscription_read_rate_limiter = RedisRateLimiter(
    key_prefix="http_subscription_read",
    max_calls=60,
    window_seconds=60,
)

# Platform admin operations (user-based)
platform_admin_rate_limiter = RedisRateLimiter(
    key_prefix="http_platform_admin",
    max_calls=60,
    window_seconds=60,
)

# Platform analytics (user-based, more permissive)
platform_analytics_rate_limiter = RedisRateLimiter(
    key_prefix="http_platform_analytics",
    max_calls=100,
    window_seconds=60,
)

# Team management (user-based)
team_management_rate_limiter = RedisRateLimiter(
    key_prefix="http_team_mgmt",
    max_calls=20,
    window_seconds=60,
)

_last_rate_limiter_error_log_at = 0.0


async def check_rate_limit(
    request: Request,
    limiter: RedisRateLimiter,
    identifier_key: str = "ip",
) -> None:
    """Check rate limit for HTTP endpoint."""
    if identifier_key == "ip":
        identifier = request.client.host if request.client else "unknown"
    elif identifier_key == "user":
        user = getattr(request.state, "user", None)
        identifier = (
            str(user.id) if user else (request.client.host if request.client else "unknown")
        )
    else:
        identifier = request.client.host if request.client else "unknown"

    try:
        usage = await limiter.get_current_usage(identifier)

        if usage["calls"] >= limiter.max_calls:
            retry_after = limiter.window_seconds

            log.warning(
                f"Rate limit exceeded for {identifier}: " f"{usage['calls']}/{limiter.max_calls}"
            )

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "Rate limit exceeded",
                    "limit": limiter.max_calls,
                    "window_seconds": limiter.window_seconds,
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        await limiter.acquire(identifier)

    except HTTPException:
        raise
    except Exception as e:
        global _last_rate_limiter_error_log_at
        now = time.time()
        cooldown = max(0, settings.RATE_LIMIT_ERROR_LOG_COOLDOWN_SECONDS)
        if now - _last_rate_limiter_error_log_at >= cooldown:
            log.error(f"Rate limiter error: {e}")
            _last_rate_limiter_error_log_at = now

        if settings.RATE_LIMIT_FAIL_OPEN:
            # Fail open by default for availability when Redis is unavailable.
            return

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Rate limiting temporarily unavailable",
        ) from e


def create_rate_limit_dependency(
    limiter: RedisRateLimiter, identifier_key: str = "ip"
) -> Callable[[Request], Awaitable[None]]:
    """Create FastAPI dependency for rate limiting."""

    async def rate_limit_dependency(request: Request) -> None:
        await check_rate_limit(request, limiter, identifier_key)

    return rate_limit_dependency


# Pre-configured dependencies
auth_rate_limit = create_rate_limit_dependency(auth_rate_limiter, "ip")
platform_admin_auth_rate_limit = create_rate_limit_dependency(
    platform_admin_auth_rate_limiter, "ip"
)
chat_rate_limit = create_rate_limit_dependency(chat_rate_limiter, "user")
upload_rate_limit = create_rate_limit_dependency(upload_rate_limiter, "user")

# Public endpoints (each with separate rate limiter)
registration_rate_limit = create_rate_limit_dependency(registration_rate_limiter, "ip")
verification_rate_limit = create_rate_limit_dependency(verification_rate_limiter, "ip")
resend_verification_rate_limit = create_rate_limit_dependency(
    resend_verification_rate_limiter, "ip"
)
webhook_rate_limit = create_rate_limit_dependency(webhook_rate_limiter, "ip")

# Authenticated endpoints
payment_rate_limit = create_rate_limit_dependency(payment_rate_limiter, "user")
subscription_rate_limit = create_rate_limit_dependency(subscription_rate_limiter, "user")
subscription_read_rate_limit = create_rate_limit_dependency(subscription_read_rate_limiter, "user")
platform_admin_rate_limit = create_rate_limit_dependency(platform_admin_rate_limiter, "user")
platform_analytics_rate_limit = create_rate_limit_dependency(
    platform_analytics_rate_limiter, "user"
)
team_management_rate_limit = create_rate_limit_dependency(team_management_rate_limiter, "user")
