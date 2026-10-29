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

# Marketer login / forgot-password / set-password: one shared per-IP budget across the three.
# Looser than the platform-admin login (3 per 15 min) because the budget is shared across the
# whole reset-then-login flow and marketers are not an admin surface; still brute-force bounded.
marketer_auth_rate_limiter = RedisRateLimiter(
    key_prefix="http_marketer_auth",
    max_calls=100 if _is_relaxed else 10,
    window_seconds=900,
)

# Marketer dashboard reads (keyed per marketer id, not IP; see endpoints/marketer.py). Generous
# for normal browsing, but bounds the cost of the referrals endpoint, which runs onboarding
# queries per in-trial row.
marketer_portal_rate_limiter = RedisRateLimiter(
    key_prefix="http_marketer_portal",
    max_calls=300 if _is_relaxed else 60,
    window_seconds=60,
)

chat_rate_limiter = RedisRateLimiter(
    key_prefix="http_chat",
    max_calls=20,
    window_seconds=60,
)

# Voice session-token minting — public, unauthenticated (like chat above),
# but a much lower ceiling than chat: each call that clears gating spends
# billable ElevenLabs quota, so the request *rate* needs its own throttle
# independent of the concurrency ceiling (which only caps how many can be
# simultaneously active, not how fast a single caller can churn through
# mint → fail → retry).
voice_rate_limiter = RedisRateLimiter(
    key_prefix="http_voice",
    max_calls=10,
    window_seconds=60,
)

# Voice custom-LLM turns — ElevenLabs calls this endpoint server-to-server
# for every tenant's every conversational turn, almost certainly from a
# small, shared pool of egress IPs. An IP- or user-keyed limit (like
# `voice_rate_limit` above) would throttle every unrelated tenant's call
# sharing that IP together. This is keyed by the already-resolved,
# unguessable voice token instead (see app/api/v1/endpoints/voice.py), so
# hitting the ceiling only throttles the one call spamming turns, not every
# voice caller on the platform.
voice_llm_turn_rate_limiter = RedisRateLimiter(
    key_prefix="http_voice_llm_turn",
    max_calls=30,
    window_seconds=60,
)

upload_rate_limiter = RedisRateLimiter(
    key_prefix="http_upload",
    max_calls=10,
    window_seconds=3600,
)

video_sign_rate_limiter = RedisRateLimiter(
    key_prefix="http_video", max_calls=5, window_seconds=3600
)
video_register_rate_limiter = RedisRateLimiter(
    key_prefix="http_video_register", max_calls=20, window_seconds=3600
)

# Public rating submit (N4): 20/hour/IP, own bucket so it never eats chat/upload quota.
rating_rate_limiter = RedisRateLimiter(key_prefix="http_rating", max_calls=20, window_seconds=3600)

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

# Public, unauthenticated account-deletion request (IP-based). Deliberately
# tight — this endpoint resolves an arbitrary email to an account/no-account
# outcome, so both abuse (spamming deletion emails at a victim) and
# enumeration-by-volume deserve a low ceiling, same posture as
# `resend_verification_rate_limiter`.
account_deletion_public_rate_limiter = RedisRateLimiter(
    key_prefix="http_account_deletion_public",
    max_calls=5,
    window_seconds=3600,  # 5 per hour
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

# WhatsApp: creating a connection triggers a pairing attempt (WhatsApp notifies the number's
# owner), so it is limited per tenant, and per target phone across ALL tenants, not just per IP.
whatsapp_create_rate_limiter = RedisRateLimiter(
    key_prefix="http_whatsapp_create",
    max_calls=10,
    window_seconds=600,
)
whatsapp_phone_rate_limiter = RedisRateLimiter(
    key_prefix="http_whatsapp_phone",
    max_calls=5,
    window_seconds=86400,
)
# New codes for one tenant's own connection to one number: keyed (tenant, number) so a tenant
# can never spend the cross-tenant per-number create budget above (victim lockout).
whatsapp_phone_pairing_rate_limiter = RedisRateLimiter(
    key_prefix="http_whatsapp_phone_pairing",
    max_calls=10,
    window_seconds=86400,
)
# ...but one number still has a hard ceiling of codes per day across ALL tenants.
whatsapp_phone_pairing_global_rate_limiter = RedisRateLimiter(
    key_prefix="http_whatsapp_phone_pairing_global",
    max_calls=20,
    window_seconds=86400,
)
whatsapp_pairing_conn_rate_limiter = RedisRateLimiter(
    key_prefix="http_whatsapp_pairing_conn",
    max_calls=5,
    window_seconds=600,
)

# WhatsApp pairing-code (re)generation: each code is a live pairing attempt on a real number
whatsapp_pairing_rate_limiter = RedisRateLimiter(
    key_prefix="http_whatsapp_pairing",
    max_calls=5,
    window_seconds=600,
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
marketer_auth_rate_limit = create_rate_limit_dependency(marketer_auth_rate_limiter, "ip")
chat_rate_limit = create_rate_limit_dependency(chat_rate_limiter, "user")
voice_rate_limit = create_rate_limit_dependency(voice_rate_limiter, "user")
upload_rate_limit = create_rate_limit_dependency(upload_rate_limiter, "user")

# Public endpoints (each with separate rate limiter)
registration_rate_limit = create_rate_limit_dependency(registration_rate_limiter, "ip")
verification_rate_limit = create_rate_limit_dependency(verification_rate_limiter, "ip")
resend_verification_rate_limit = create_rate_limit_dependency(
    resend_verification_rate_limiter, "ip"
)
webhook_rate_limit = create_rate_limit_dependency(webhook_rate_limiter, "ip")
whatsapp_pairing_rate_limit = create_rate_limit_dependency(whatsapp_pairing_rate_limiter, "ip")
# Public (unauthenticated) chat image upload: key by IP, not user.
chat_upload_rate_limit = create_rate_limit_dependency(upload_rate_limiter, "ip")
# Public screen-recording endpoints (separate buckets so videos never consume image uploads).
chat_video_sign_rate_limit = create_rate_limit_dependency(video_sign_rate_limiter, "ip")
chat_video_register_rate_limit = create_rate_limit_dependency(video_register_rate_limiter, "ip")


async def rating_rate_limit(request: Request) -> None:
    """Public POST /reviews/session limiter; 429 uses the N4 object error shape."""
    try:
        await check_rate_limit(request, rating_rate_limiter, "ip")
    except HTTPException as exc:
        if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
            raise
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rating_rate_limited",
                "message": "Too many ratings from this network. Try again later",
                "retry_after_seconds": rating_rate_limiter.window_seconds,
            },
            headers=exc.headers,
        ) from exc


# No `create_rate_limit_dependency` wrapper for account_deletion_public: the
# deletion-request endpoint is dual-mode (authed vs public) and must only
# rate-limit the public branch, so it calls `check_rate_limit` directly
# inside that branch instead of via a blanket `Depends`.

# Authenticated endpoints
payment_rate_limit = create_rate_limit_dependency(payment_rate_limiter, "user")
subscription_rate_limit = create_rate_limit_dependency(subscription_rate_limiter, "user")
subscription_read_rate_limit = create_rate_limit_dependency(subscription_read_rate_limiter, "user")
platform_admin_rate_limit = create_rate_limit_dependency(platform_admin_rate_limiter, "user")
platform_analytics_rate_limit = create_rate_limit_dependency(
    platform_analytics_rate_limiter, "user"
)
team_management_rate_limit = create_rate_limit_dependency(team_management_rate_limiter, "user")


async def enforce_identifier_limit(limiter: RedisRateLimiter, identifier: str) -> None:
    """Rate limit on an arbitrary key (tenant id, phone hash, ...) instead of the caller IP.

    Same contract as `check_rate_limit`: 429 when exceeded, fail-open on Redis errors unless
    RATE_LIMIT_FAIL_OPEN is off.
    """
    try:
        usage = await limiter.get_current_usage(identifier)
        if usage["calls"] >= limiter.max_calls:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "Rate limit exceeded",
                    "limit": limiter.max_calls,
                    "window_seconds": limiter.window_seconds,
                    "retry_after_seconds": limiter.window_seconds,
                },
                headers={"Retry-After": str(limiter.window_seconds)},
            )
        await limiter.acquire(identifier)
    except HTTPException:
        raise
    except Exception:
        if settings.RATE_LIMIT_FAIL_OPEN:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Rate limiting temporarily unavailable",
        ) from None
