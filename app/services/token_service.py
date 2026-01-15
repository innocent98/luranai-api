"""Token service for managing verification and reset tokens."""

import secrets
from typing import Optional
from uuid import UUID

import redis
from loguru import logger

from app.core.config import settings


class TokenService:
    """Service for managing temporary tokens in Redis."""

    def __init__(self):
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

    def verify_email_token(self, token: str) -> Optional[dict]:
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
                logger.warning(f"Invalid or expired email verification token")
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

    def verify_password_reset_token(self, token: str) -> Optional[dict]:
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
                logger.warning(f"Invalid or expired password reset token")
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

    def verify_platform_admin_reset_token(self, token: str) -> Optional[dict]:
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
                logger.warning(f"Invalid or expired platform admin reset token")
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

    def invalidate_all_user_tokens(self, user_id: UUID) -> None:
        """
        Invalidate all tokens for a specific user.

        Args:
            user_id: User ID to invalidate tokens for
        """
        try:
            # Find all keys related to this user
            patterns = [
                f"email_verification:*",
                f"password_reset:*",
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
