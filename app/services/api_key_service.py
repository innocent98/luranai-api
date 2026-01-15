"""API key management service."""

import secrets
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select
from loguru import logger

from app.core.security import get_password_hash, verify_password
from app.db.models.api_key import APIKey, APIKeyType
from app.schemas.api_key import (
    APIKeyCreate,
    APIKeyCreateResponse,
    APIKeyListResponse,
    APIKeyUpdate,
)


class APIKeyService:
    """Service for managing API keys."""

    def __init__(self, db: Session):
        """Initialize API key service with database session."""
        self.db = db

    def _generate_api_key(self, key_type: APIKeyType) -> tuple[str, str, str]:
        """
        Generate a new API key.

        Args:
            key_type: Type of API key (PUBLIC or SECRET)

        Returns:
            Tuple of (full_key, key_prefix, key_hash)
        """
        # Generate random key (32 bytes = 256 bits)
        random_key = secrets.token_urlsafe(32)

        # Create prefix based on type
        prefix = "pk_live_" if key_type == APIKeyType.PUBLIC else "sk_live_"

        # Full key
        full_key = f"{prefix}{random_key}"

        # Key prefix for display (first 15 characters)
        key_prefix = full_key[:15] + "..."

        # Hash the full key for storage
        key_hash = get_password_hash(full_key)

        return full_key, key_prefix, key_hash

    def create_api_key(
        self, tenant_id: UUID, key_data: APIKeyCreate
    ) -> APIKeyCreateResponse:
        """
        Create a new API key for a tenant.

        Args:
            tenant_id: Tenant UUID
            key_data: API key creation data

        Returns:
            Created API key response with full key (shown only once)
        """
        # Generate API key
        full_key, key_prefix, key_hash = self._generate_api_key(key_data.key_type)

        try:
            # Create API key record
            api_key = APIKey(
                tenant_id=tenant_id,
                key_type=key_data.key_type,
                key_prefix=key_prefix,
                key_hash=key_hash,
                name=key_data.name,
                is_active=True,
                expires_at=key_data.expires_at,
            )

            self.db.add(api_key)
            self.db.commit()
            self.db.refresh(api_key)

            logger.info(
                f"Created API key '{key_data.name}' for tenant {tenant_id} "
                f"(type={key_data.key_type.value})"
            )

            # Return response with full key (only time it's shown)
            return APIKeyCreateResponse(
                id=api_key.id,
                tenant_id=api_key.tenant_id,
                key_type=api_key.key_type,
                key_prefix=api_key.key_prefix,
                name=api_key.name,
                is_active=api_key.is_active,
                last_used_at=api_key.last_used_at,
                expires_at=api_key.expires_at,
                created_at=api_key.created_at,
                updated_at=api_key.updated_at,
                full_key=full_key,
            )

        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to create API key: {e}")
            raise

    def verify_api_key(self, api_key: str) -> Optional[APIKey]:
        """
        Verify an API key and return the associated key record.

        Args:
            api_key: Full API key to verify

        Returns:
            APIKey record if valid, None otherwise
        """
        # Extract prefix to determine key type
        if api_key.startswith("pk_live_"):
            key_type = APIKeyType.PUBLIC
        elif api_key.startswith("sk_live_"):
            key_type = APIKeyType.SECRET
        else:
            return None

        # Get all active keys of this type
        stmt = select(APIKey).where(
            APIKey.key_type == key_type, APIKey.is_active == True
        )
        keys = self.db.execute(stmt).scalars().all()

        # Verify key against each stored hash
        for key_record in keys:
            if verify_password(api_key, key_record.key_hash):
                # Check expiration
                if key_record.expires_at and key_record.expires_at < datetime.utcnow():
                    logger.warning(f"API key {key_record.id} has expired")
                    return None

                # Update last used timestamp
                key_record.last_used_at = datetime.utcnow()
                self.db.commit()

                return key_record

        return None

    def list_api_keys(self, tenant_id: UUID) -> list[APIKeyListResponse]:
        """
        List all API keys for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            List of API key responses
        """
        stmt = select(APIKey).where(APIKey.tenant_id == tenant_id)
        keys = self.db.execute(stmt).scalars().all()

        return [APIKeyListResponse.model_validate(key) for key in keys]

    def get_api_key(self, key_id: UUID, tenant_id: UUID) -> Optional[APIKey]:
        """
        Get API key by ID for a tenant.

        Args:
            key_id: API key UUID
            tenant_id: Tenant UUID (for access control)

        Returns:
            APIKey record if found, None otherwise
        """
        stmt = select(APIKey).where(APIKey.id == key_id, APIKey.tenant_id == tenant_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def update_api_key(
        self, key_id: UUID, tenant_id: UUID, update_data: APIKeyUpdate
    ) -> Optional[APIKey]:
        """
        Update an API key.

        Args:
            key_id: API key UUID
            tenant_id: Tenant UUID (for access control)
            update_data: Update data

        Returns:
            Updated APIKey record if found, None otherwise
        """
        # Get key
        key = self.get_api_key(key_id, tenant_id)
        if not key:
            return None

        # Update fields
        if update_data.name is not None:
            key.name = update_data.name

        if update_data.is_active is not None:
            key.is_active = update_data.is_active

        self.db.commit()
        self.db.refresh(key)

        logger.info(f"Updated API key {key_id}")

        return key

    def delete_api_key(self, key_id: UUID, tenant_id: UUID) -> bool:
        """
        Delete an API key.

        Args:
            key_id: API key UUID
            tenant_id: Tenant UUID (for access control)

        Returns:
            True if deleted, False if not found
        """
        key = self.get_api_key(key_id, tenant_id)
        if not key:
            return False

        self.db.delete(key)
        self.db.commit()

        logger.info(f"Deleted API key {key_id} for tenant {tenant_id}")

        return True
