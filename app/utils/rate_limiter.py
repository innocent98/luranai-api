"""
Redis-based rate limiter for API calls.

This rate limiter uses Redis for persistent state, ensuring rate limits
work across multiple processes and survive application restarts.
"""

import asyncio
import time
from typing import Optional

import redis.asyncio as aioredis
from loguru import logger

from app.core.config import settings


class RedisRateLimiter:
    """
    Redis-based rate limiter using sliding window algorithm.

    Ensures rate limits are enforced across all processes and persist
    through application restarts.
    """

    def __init__(
        self,
        key_prefix: str,
        max_calls: int,
        window_seconds: int = 60,
        redis_url: Optional[str] = None,
    ):
        """
        Initialize rate limiter.

        Args:
            key_prefix: Redis key prefix (e.g., "embedding_api")
            max_calls: Maximum calls allowed in the time window
            window_seconds: Time window in seconds (default: 60)
            redis_url: Redis connection URL (defaults to settings.REDIS_URL)
        """
        self.key_prefix = key_prefix
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: Optional[aioredis.Redis] = None

    async def _get_redis(self) -> aioredis.Redis:
        """Get or create Redis connection."""
        if self._redis is None:
            self._redis = await aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._redis

    async def acquire(self, identifier: str = "default") -> None:
        """
        Acquire permission to make an API call.

        Blocks until a slot is available within rate limit.

        Args:
            identifier: Optional identifier for separate rate limits
                       (e.g., different API keys, tenants)
        """
        redis = await self._get_redis()
        key = f"{self.key_prefix}:{identifier}"

        while True:
            now = time.time()
            window_start = now - self.window_seconds

            # Remove expired entries (older than window)
            await redis.zremrangebyscore(key, "-inf", window_start)

            # Count current calls in window
            call_count = await redis.zcard(key)

            if call_count < self.max_calls:
                # Add current timestamp to sorted set
                await redis.zadd(key, {str(now): now})

                # Set expiration to clean up old keys
                await redis.expire(key, self.window_seconds + 10)

                logger.debug(
                    f"Rate limit [{self.key_prefix}]: {call_count + 1}/{self.max_calls} "
                    f"calls in last {self.window_seconds}s"
                )
                return

            # Rate limit exceeded - calculate wait time
            oldest_call = await redis.zrange(key, 0, 0, withscores=True)
            if oldest_call:
                oldest_timestamp = oldest_call[0][1]
                wait_time = self.window_seconds - (now - oldest_timestamp) + 1

                if wait_time > 0:
                    logger.warning(
                        f"Rate limit reached ({self.max_calls}/{self.window_seconds}s). "
                        f"Waiting {wait_time:.1f}s..."
                    )
                    await asyncio.sleep(wait_time)
            else:
                # Shouldn't happen, but just in case
                await asyncio.sleep(1)

    async def get_current_usage(self, identifier: str = "default") -> dict:
        """
        Get current rate limit usage.

        Args:
            identifier: Optional identifier

        Returns:
            Dict with usage stats: {calls, max_calls, window_seconds, usage_percent}
        """
        redis = await self._get_redis()
        key = f"{self.key_prefix}:{identifier}"

        now = time.time()
        window_start = now - self.window_seconds

        # Remove expired entries
        await redis.zremrangebyscore(key, "-inf", window_start)

        # Count current calls
        call_count = await redis.zcard(key)
        usage_percent = (call_count / self.max_calls) * 100 if self.max_calls > 0 else 0

        return {
            "calls": call_count,
            "max_calls": self.max_calls,
            "window_seconds": self.window_seconds,
            "usage_percent": round(usage_percent, 2),
            "available": self.max_calls - call_count,
        }

    async def reset(self, identifier: str = "default") -> None:
        """
        Reset rate limit counter for identifier.

        Args:
            identifier: Optional identifier
        """
        redis = await self._get_redis()
        key = f"{self.key_prefix}:{identifier}"
        await redis.delete(key)
        logger.info(f"Rate limit reset for {key}")

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()


class QuotaCircuitBreaker:
    """
    Circuit breaker for API quotas.

    Stops making requests when quota is exhausted or approaching limit,
    preventing wasted API calls and automatic backoff.
    """

    def __init__(
        self,
        rate_limiter: RedisRateLimiter,
        warning_threshold: float = 0.8,
        block_threshold: float = 0.95,
    ):
        """
        Initialize circuit breaker.

        Args:
            rate_limiter: Associated rate limiter to monitor
            warning_threshold: Warn when usage exceeds this % (default: 80%)
            block_threshold: Block requests when usage exceeds this % (default: 95%)
        """
        self.rate_limiter = rate_limiter
        self.warning_threshold = warning_threshold
        self.block_threshold = block_threshold
        self._circuit_open = False

    async def check_and_acquire(self, identifier: str = "default") -> None:
        """
        Check quota status and acquire if safe.

        Raises:
            RuntimeError: If circuit is open (quota exhausted)
        """
        # Get current usage
        usage = await self.rate_limiter.get_current_usage(identifier)
        usage_percent = usage["usage_percent"]

        # Check if circuit should be opened (quota nearly exhausted)
        if usage_percent >= self.block_threshold * 100:
            self._circuit_open = True
            remaining_time = self.rate_limiter.window_seconds - (
                usage["calls"] / self.rate_limiter.max_calls * self.rate_limiter.window_seconds
            )
            raise RuntimeError(
                f"Circuit breaker OPEN: Quota {usage_percent:.1f}% used "
                f"({usage['calls']}/{usage['max_calls']}). "
                f"Blocking requests to prevent quota exhaustion. "
                f"Quota resets in ~{remaining_time/3600:.1f} hours."
            )

        # Warn if approaching threshold
        if usage_percent >= self.warning_threshold * 100:
            logger.warning(
                f"⚠️  Quota usage high: {usage_percent:.1f}% "
                f"({usage['calls']}/{usage['max_calls']}). "
                f"Only {usage['available']} requests remaining."
            )

        # Acquire normally if under threshold
        await self.rate_limiter.acquire(identifier)

    def is_open(self) -> bool:
        """Check if circuit is open (blocking requests)."""
        return self._circuit_open

    def reset_circuit(self) -> None:
        """Manually reset circuit (use after quota reset)."""
        self._circuit_open = False
        logger.info("Circuit breaker manually reset")


# Global rate limiter instances
# OpenAI Embedding API: text-embedding-3-small
# Official limits: 3,000 RPM (Tier 1)
# We use 2,700 RPM to stay safe (90% of limit)
openai_embedding_rate_limiter = RedisRateLimiter(
    key_prefix="openai_embedding_api",
    max_calls=2700,  # 90% of 3,000 RPM
    window_seconds=60,
)

# OpenAI Chat API: gpt-4o-mini
# Official limits: 500 RPM (Tier 1)
# We use 450 RPM to stay safe (90% of limit)
openai_llm_rate_limiter = RedisRateLimiter(
    key_prefix="openai_llm_api",
    max_calls=450,  # 90% of 500 RPM
    window_seconds=60,
)
