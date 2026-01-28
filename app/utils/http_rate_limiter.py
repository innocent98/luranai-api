"""HTTP endpoint rate limiting using Redis."""

from typing import Optional
from fastapi import HTTPException, Request, status
from app.utils.rate_limiter import RedisRateLimiter
from app.core.logger import log


# Rate limiters for different endpoint types
auth_rate_limiter = RedisRateLimiter(
    key_prefix="http_auth",
    max_calls=5,
    window_seconds=60,
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

# Public registration endpoints (IP-based, aggressive)
registration_rate_limiter = RedisRateLimiter(
    key_prefix="http_registration",
    max_calls=3,
    window_seconds=3600,  # 3 per hour
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


async def check_rate_limit(
    request: Request,
    limiter: RedisRateLimiter,
    identifier_key: str = "ip",
) -> None:
    """Check rate limit for HTTP endpoint."""
    if identifier_key == "ip":
        identifier = request.client.host
    elif identifier_key == "user":
        user = getattr(request.state, "user", None)
        identifier = str(user.id) if user else request.client.host
    else:
        identifier = request.client.host

    try:
        usage = await limiter.get_current_usage(identifier)

        if usage["calls"] >= limiter.max_calls:
            retry_after = limiter.window_seconds

            log.warning(
                f"Rate limit exceeded for {identifier}: "
                f"{usage['calls']}/{limiter.max_calls}"
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
        log.error(f"Rate limiter error: {e}")
        # Fail open - don't block if Redis down
        pass


def create_rate_limit_dependency(
    limiter: RedisRateLimiter,
    identifier_key: str = "ip"
):
    """Create FastAPI dependency for rate limiting."""
    async def rate_limit_dependency(request: Request):
        await check_rate_limit(request, limiter, identifier_key)
    return rate_limit_dependency


# Pre-configured dependencies
auth_rate_limit = create_rate_limit_dependency(auth_rate_limiter, "ip")
chat_rate_limit = create_rate_limit_dependency(chat_rate_limiter, "user")
upload_rate_limit = create_rate_limit_dependency(upload_rate_limiter, "user")

# Public endpoints
registration_rate_limit = create_rate_limit_dependency(registration_rate_limiter, "ip")
webhook_rate_limit = create_rate_limit_dependency(webhook_rate_limiter, "ip")

# Authenticated endpoints
payment_rate_limit = create_rate_limit_dependency(payment_rate_limiter, "user")
subscription_rate_limit = create_rate_limit_dependency(subscription_rate_limiter, "user")
platform_admin_rate_limit = create_rate_limit_dependency(platform_admin_rate_limiter, "user")
platform_analytics_rate_limit = create_rate_limit_dependency(platform_analytics_rate_limiter, "user")
team_management_rate_limit = create_rate_limit_dependency(team_management_rate_limiter, "user")
