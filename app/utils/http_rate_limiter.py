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
