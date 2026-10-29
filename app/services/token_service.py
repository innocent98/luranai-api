"""Token service for managing verification and reset tokens."""

import secrets
from uuid import UUID

import redis
from loguru import logger

from app.core.config import settings


class TokenService:
    """Service for managing temporary tokens in Redis."""

    def __init__(self) -> None:
        """Initialize Redis connection."""
        self.redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )

    def _generate_token(self, length: int = 32) -> str:
        """Generate a secure random token."""
        return secrets.token_urlsafe(length)

    # Email verification tokens
    def create_email_verification_token(
        self,
        user_id: UUID,
        email: str,
        expiry_hours: int = 24,
    ) -> str:
        """
        Create an email verification token.

        Args:
            user_id: UUID of the user
            email: Email address to verify
            expiry_hours: Token expiry in hours (default 24)

        Returns:
            Generated token string
        """
        token = self._generate_token()
        key = f"email_verification:{token}"

        # Store user_id and email in Redis
        data = {
            "user_id": str(user_id),
            "email": email,
        }

        try:
            # Set with expiration
            self.redis_client.hset(key, mapping=data)
            self.redis_client.expire(key, expiry_hours * 3600)
            logger.info(f"Created email verification token for user {user_id}")
            return token
        except Exception as e:
            logger.error(f"Failed to create email verification token: {e}")
            raise

    def verify_email_token(self, token: str) -> dict | None:
        """
        Verify an email verification token.

        Args:
            token: Token to verify

        Returns:
            Dict with user_id and email if valid, None otherwise
        """
        key = f"email_verification:{token}"

        try:
            data = self.redis_client.hgetall(key)
            if not data:
                logger.warning("Invalid or expired email verification token")
                return None

            # Delete token after verification (one-time use)
            self.redis_client.delete(key)

            return {
                "user_id": UUID(data["user_id"]),
                "email": data["email"],
            }
        except Exception as e:
            logger.error(f"Failed to verify email token: {e}")
            return None

    def create_customer_email_verification_token(
        self,
        customer_user_id: UUID,
        email: str,
        expiry_hours: int = 24,
    ) -> str:
        token = self._generate_token()
        key = f"customer_email_verification:{token}"

        data = {
            "customer_user_id": str(customer_user_id),
            "email": email,
        }

        self.redis_client.hset(key, mapping=data)
        self.redis_client.expire(key, expiry_hours * 3600)
        return token

    def verify_customer_email_token(self, token: str) -> dict | None:
        key = f"customer_email_verification:{token}"

        try:
            data = self.redis_client.hgetall(key)
            if not data:
                return None
            self.redis_client.delete(key)
            return {
                "customer_user_id": UUID(data["customer_user_id"]),
                "email": data["email"],
            }
        except Exception as e:
            logger.error(f"Failed to verify customer email token: {e}")
            return None

    # Password reset tokens
    def create_password_reset_token(
        self,
        user_id: UUID,
        email: str,
        expiry_minutes: int = 15,
    ) -> str:
        """
        Create a password reset token.

        Args:
            user_id: UUID of the user
            email: User's email address
            expiry_minutes: Token expiry in minutes (default 15)

        Returns:
            Generated token string
        """
        token = self._generate_token()
        key = f"password_reset:{token}"

        data = {
            "user_id": str(user_id),
            "email": email,
        }

        try:
            self.redis_client.hset(key, mapping=data)
            self.redis_client.expire(key, expiry_minutes * 60)
            logger.info(f"Created password reset token for user {user_id}")
            return token
        except Exception as e:
            logger.error(f"Failed to create password reset token: {e}")
            raise

    def verify_password_reset_token(self, token: str) -> dict | None:
        """
        Verify a password reset token.

        Args:
            token: Token to verify

        Returns:
            Dict with user_id and email if valid, None otherwise
        """
        key = f"password_reset:{token}"

        try:
            data = self.redis_client.hgetall(key)
            if not data:
                logger.warning("Invalid or expired password reset token")
                return None

            # Delete token after verification (one-time use)
            self.redis_client.delete(key)

            return {
                "user_id": UUID(data["user_id"]),
                "email": data["email"],
            }
        except Exception as e:
            logger.error(f"Failed to verify password reset token: {e}")
            return None

    def create_customer_password_reset_token(
        self,
        customer_user_id: UUID,
        email: str,
        expiry_minutes: int = 15,
    ) -> str:
        token = self._generate_token()
        key = f"customer_password_reset:{token}"
        data = {
            "customer_user_id": str(customer_user_id),
            "email": email,
        }
        self.redis_client.hset(key, mapping=data)
        self.redis_client.expire(key, expiry_minutes * 60)
        return token

    def verify_customer_password_reset_token(self, token: str) -> dict | None:
        key = f"customer_password_reset:{token}"

        try:
            data = self.redis_client.hgetall(key)
            if not data:
                return None
            self.redis_client.delete(key)
            return {
                "customer_user_id": UUID(data["customer_user_id"]),
                "email": data["email"],
            }
        except Exception as e:
            logger.error(f"Failed to verify customer password reset token: {e}")
            return None

    # Platform admin password reset tokens
    def create_platform_admin_reset_token(
        self,
        admin_id: UUID,
        email: str,
        expiry_minutes: int = 15,
    ) -> str:
        """
        Create a password reset token for platform admin.

        Args:
            admin_id: UUID of the platform admin
            email: Admin's email address
            expiry_minutes: Token expiry in minutes (default 15)

        Returns:
            Generated token string
        """
        token = self._generate_token()
        key = f"platform_admin_reset:{token}"

        data = {
            "admin_id": str(admin_id),
            "email": email,
        }

        try:
            self.redis_client.hset(key, mapping=data)
            self.redis_client.expire(key, expiry_minutes * 60)
            logger.info(f"Created password reset token for platform admin {admin_id}")
            return token
        except Exception as e:
            logger.error(f"Failed to create platform admin reset token: {e}")
            raise

    def verify_platform_admin_reset_token(self, token: str) -> dict | None:
        """
        Verify a platform admin password reset token.

        Args:
            token: Token to verify

        Returns:
            Dict with admin_id and email if valid, None otherwise
        """
        key = f"platform_admin_reset:{token}"

        try:
            data = self.redis_client.hgetall(key)
            if not data:
                logger.warning("Invalid or expired platform admin reset token")
                return None

            # Delete token after verification (one-time use)
            self.redis_client.delete(key)

            return {
                "admin_id": UUID(data["admin_id"]),
                "email": data["email"],
            }
        except Exception as e:
            logger.error(f"Failed to verify platform admin reset token: {e}")
            return None

    # Marketer invite tokens (set-password link emailed when an admin onboards a marketer)
    def create_marketer_invite_token(
        self,
        marketer_id: UUID,
        email: str,
        expiry_minutes: int = 7 * 24 * 60,
    ) -> str:
        """
        Create a one-time invite token a marketer redeems to set their first password.

        Args:
            marketer_id: UUID of the marketer
            email: Marketer's (normalised) email address
            expiry_minutes: Token expiry in minutes (default 7 days)

        Returns:
            Generated token string
        """
        token = self._generate_token()
        key = f"marketer_invite:{token}"

        data = {
            "marketer_id": str(marketer_id),
            "email": email,
        }

        index_key = self._marketer_index_key(marketer_id)
        ttl = expiry_minutes * 60

        try:
            # Track every live token per marketer so they can all be killed at once (see
            # invalidate_marketer_tokens). The index outlives its longest-lived member.
            index_ttl = max(int(self.redis_client.ttl(index_key) or 0), ttl)
            pipe = self.redis_client.pipeline(transaction=True)
            pipe.hset(key, mapping=data)
            pipe.expire(key, ttl)
            pipe.sadd(index_key, key)
            pipe.expire(index_key, index_ttl)
            pipe.execute()
            logger.info(f"Created invite token for marketer {marketer_id}")
            return token
        except Exception as e:
            logger.error(f"Failed to create marketer invite token: {e}")
            raise

    @staticmethod
    def _marketer_index_key(marketer_id: UUID) -> str:
        return f"marketer_tokens:{marketer_id}"

    # Deletes every token key listed in the marketer's index, then the index, atomically.
    _INVALIDATE_MARKETER_TOKENS = """
    local keys = redis.call('SMEMBERS', KEYS[1])
    for _, k in ipairs(keys) do redis.call('DEL', k) end
    redis.call('DEL', KEYS[1])
    return #keys
    """

    def acquire_marketer_reset_cooldown(self, marketer_id: UUID, seconds: int = 120) -> bool:
        """
        Claim the per-marketer reset-email cooldown (atomic SET NX EX).

        Returns:
            True if the caller may send a reset email now, False if one was issued within
            the last ``seconds`` seconds.
        """
        try:
            return bool(
                self.redis_client.set(
                    f"marketer_reset_cooldown:{marketer_id}", "1", nx=True, ex=seconds
                )
            )
        except Exception as e:
            logger.error(f"Failed to set marketer reset cooldown for {marketer_id}: {e}")
            raise

    def invalidate_marketer_tokens(self, marketer_id: UUID) -> int:
        """
        Delete ALL of a marketer's outstanding invite / reset tokens.

        Called only after a password is successfully set (an authenticated-by-token action).
        Never call it from an unauthenticated path: anyone who knows the email could otherwise
        kill an admin-issued invite.

        Returns:
            Number of tokens deleted (stale index entries included)
        """
        try:
            return int(
                self.redis_client.eval(
                    self._INVALIDATE_MARKETER_TOKENS, 1, self._marketer_index_key(marketer_id)
                )
            )
        except Exception as e:
            logger.error(f"Failed to invalidate marketer tokens for {marketer_id}: {e}")
            raise

    def verify_marketer_invite_token(self, token: str) -> dict | None:
        """
        Verify a marketer invite token and consume it (one-time use).

        Args:
            token: Token to verify

        Returns:
            Dict with marketer_id and email if valid, None otherwise
        """
        key = f"marketer_invite:{token}"

        try:
            # Read and delete in one MULTI/EXEC so two concurrent redemptions cannot both win.
            pipe = self.redis_client.pipeline(transaction=True)
            pipe.hgetall(key)
            pipe.delete(key)
            data, _ = pipe.execute()
            if not data:
                logger.warning("Invalid or expired marketer invite token")
                return None

            return {
                "marketer_id": UUID(data["marketer_id"]),
                "email": data["email"],
            }
        except Exception as e:
            logger.error(f"Failed to verify marketer invite token: {e}")
            return None

    def invalidate_all_user_tokens(self, user_id: UUID) -> None:
        """
        Invalidate all tokens for a specific user.

        Args:
            user_id: User ID to invalidate tokens for
        """
        try:
            # Find all keys related to this user
            patterns = [
                "email_verification:*",
                "password_reset:*",
            ]

            for pattern in patterns:
                for key in self.redis_client.scan_iter(match=pattern):
                    data = self.redis_client.hgetall(key)
                    if data.get("user_id") == str(user_id):
                        self.redis_client.delete(key)
                        logger.info(f"Invalidated token {key} for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to invalidate user tokens: {e}")


# Singleton instance
token_service = TokenService()
