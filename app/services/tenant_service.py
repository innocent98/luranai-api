"""Tenant service for managing tenants, AI configs, and capabilities."""

from typing import Optional
from uuid import UUID
import secrets

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.security import get_password_hash
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities, TenantStatus
from app.schemas.tenant import (
    TenantCreate,
    TenantUpdate,
    TenantResponse,
    TenantListResponse,
    TenantAIConfigCreate,
    TenantAIConfigUpdate,
    TenantAIConfigResponse,
    TenantCapabilitiesCreate,
    TenantCapabilitiesUpdate,
    TenantCapabilitiesResponse,
)


class TenantService:
    """Service for tenant management operations."""

    def __init__(self, db: Session):
        """Initialize tenant service with database session."""
        self.db = db

    def create_tenant(self, tenant_data: TenantCreate) -> TenantResponse:
        """
        Create a new tenant with default AI config and capabilities.

        Args:
            tenant_data: Tenant creation data

        Returns:
            Created tenant with relationships (includes generated API key)

        Raises:
            ValueError: If tenant with name already exists
        """
        # Check if tenant exists
        stmt = select(Tenant).where(Tenant.name == tenant_data.name)
        existing = self.db.execute(stmt).scalar_one_or_none()

        if existing:
            raise ValueError(f"Tenant with name '{tenant_data.name}' already exists")

        # Generate API key (32 bytes = 64 hex characters)
        api_key = secrets.token_urlsafe(32)

        # Hash API key for storage
        api_key_hash = get_password_hash(api_key)

        # Create tenant
        db_tenant = Tenant(
            name=tenant_data.name,
            display_name=tenant_data.display_name,
            description=tenant_data.description,
            status=tenant_data.status,
            api_key_hash=api_key_hash,
        )

        self.db.add(db_tenant)
        self.db.flush()  # Get tenant ID

        # Create default AI config
        ai_config = TenantAIConfig(
            tenant_id=db_tenant.id,
            brand_voice={},
            escalation_keywords=[],
            confidence_threshold=0.55,
            max_response_tokens=400,
            grounding_requirement=True,
        )
        self.db.add(ai_config)

        # Create default capabilities (trial tier)
        capabilities = TenantCapabilities(
            tenant_id=db_tenant.id,
            can_create_tickets=True,
            can_access_analytics=False,  # No analytics in trial
            can_upload_knowledge=True,
            max_knowledge_mb=10,  # 10 MB for trial
            max_conversations_per_month=100,  # 100 conversations/month for trial
            max_knowledge_sources=2,  # Only 2 sources for trial
            can_use_url_sources=False,  # Files only, no URLs in trial
            max_team_members=1,  # Only 1 user (admin) for trial
        )
        self.db.add(capabilities)

        self.db.commit()
        self.db.refresh(db_tenant)

        # Return tenant with the generated API key (only shown once at creation)
        response = TenantResponse.model_validate(db_tenant)
        response.api_key = api_key  # Add the plain API key to response
        return response

    def get_tenant(self, tenant_id: UUID) -> Optional[Tenant]:
        """
        Get raw tenant model by ID.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Tenant model if found, None otherwise
        """
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_tenant_by_id(self, tenant_id: UUID, include_relations: bool = True) -> Optional[TenantResponse]:
        """
        Get tenant by ID.

        Args:
            tenant_id: Tenant UUID
            include_relations: Whether to include AI config and capabilities

        Returns:
            Tenant response if found, None otherwise
        """
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        tenant = self.db.execute(stmt).scalar_one_or_none()

        if not tenant:
            return None

        if include_relations:
            return TenantResponse.model_validate(tenant)
        else:
            return TenantListResponse.model_validate(tenant)

    def get_tenant_by_name(self, name: str) -> Optional[TenantResponse]:
        """
        Get tenant by name.

        Args:
            name: Tenant name

        Returns:
            Tenant response if found, None otherwise
        """
        stmt = select(Tenant).where(Tenant.name == name)
        tenant = self.db.execute(stmt).scalar_one_or_none()

        if not tenant:
            return None

        return TenantResponse.model_validate(tenant)

    def get_all_tenants(self, status: Optional[TenantStatus] = None) -> list[TenantListResponse]:
        """
        Get all tenants, optionally filtered by status.

        Args:
            status: Optional status filter

        Returns:
            List of tenant responses
        """
        stmt = select(Tenant)

        if status:
            stmt = stmt.where(Tenant.status == status)

        stmt = stmt.order_by(Tenant.created_at.desc())
        tenants = self.db.execute(stmt).scalars().all()

        return [TenantListResponse.model_validate(t) for t in tenants]

    def update_tenant(self, tenant_id: UUID, tenant_data: TenantUpdate) -> TenantResponse:
        """
        Update tenant information.

        Args:
            tenant_id: Tenant UUID
            tenant_data: Updated tenant data

        Returns:
            Updated tenant response

        Raises:
            ValueError: If tenant not found
        """
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        tenant = self.db.execute(stmt).scalar_one_or_none()

        if not tenant:
            raise ValueError("Tenant not found")

        # Update fields
        update_data = tenant_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(tenant, field, value)

        self.db.commit()
        self.db.refresh(tenant)

        return TenantResponse.model_validate(tenant)

    def delete_tenant(self, tenant_id: UUID) -> bool:
        """
        Delete a tenant (soft delete by setting status to inactive).

        Args:
            tenant_id: Tenant UUID

        Returns:
            True if deleted successfully

        Raises:
            ValueError: If tenant not found
        """
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        tenant = self.db.execute(stmt).scalar_one_or_none()

        if not tenant:
            raise ValueError("Tenant not found")

        tenant.status = TenantStatus.INACTIVE
        self.db.commit()

        return True

    # AI Config methods

    def get_ai_config(self, tenant_id: UUID) -> Optional[TenantAIConfigResponse]:
        """
        Get AI configuration for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            AI config response if found, None otherwise
        """
        stmt = select(TenantAIConfig).where(TenantAIConfig.tenant_id == tenant_id)
        config = self.db.execute(stmt).scalar_one_or_none()

        if not config:
            return None

        return TenantAIConfigResponse.model_validate(config)

    def update_ai_config(self, tenant_id: UUID, config_data: TenantAIConfigUpdate) -> TenantAIConfigResponse:
        """
        Update AI configuration for a tenant.

        Args:
            tenant_id: Tenant UUID
            config_data: Updated config data

        Returns:
            Updated AI config response

        Raises:
            ValueError: If AI config not found
        """
        stmt = select(TenantAIConfig).where(TenantAIConfig.tenant_id == tenant_id)
        config = self.db.execute(stmt).scalar_one_or_none()

        if not config:
            raise ValueError("AI config not found for tenant")

        # Update fields
        update_data = config_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(config, field, value)

        self.db.commit()
        self.db.refresh(config)

        return TenantAIConfigResponse.model_validate(config)

    # Capabilities methods

    def get_capabilities(self, tenant_id: UUID) -> Optional[TenantCapabilitiesResponse]:
        """
        Get capabilities for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Capabilities response if found, None otherwise
        """
        stmt = select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        capabilities = self.db.execute(stmt).scalar_one_or_none()

        if not capabilities:
            return None

        return TenantCapabilitiesResponse.model_validate(capabilities)

    def update_capabilities(
        self, tenant_id: UUID, capabilities_data: TenantCapabilitiesUpdate
    ) -> TenantCapabilitiesResponse:
        """
        Update capabilities for a tenant.

        Args:
            tenant_id: Tenant UUID
            capabilities_data: Updated capabilities data

        Returns:
            Updated capabilities response

        Raises:
            ValueError: If capabilities not found
        """
        stmt = select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        capabilities = self.db.execute(stmt).scalar_one_or_none()

        if not capabilities:
            raise ValueError("Capabilities not found for tenant")

        # Update fields
        update_data = capabilities_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(capabilities, field, value)

        self.db.commit()
        self.db.refresh(capabilities)

        return TenantCapabilitiesResponse.model_validate(capabilities)

    def verify_api_key(self, name: str, api_key: str) -> Optional[UUID]:
        """
        Verify tenant API key and return tenant ID if valid.

        Args:
            name: Tenant name
            api_key: Plain text API key

        Returns:
            Tenant UUID if valid, None otherwise
        """
        from app.core.security import verify_password

        stmt = select(Tenant).where(Tenant.name == name, Tenant.status == TenantStatus.ACTIVE)
        tenant = self.db.execute(stmt).scalar_one_or_none()

        if not tenant:
            return None

        if not verify_password(api_key, tenant.api_key_hash):
            return None

        return tenant.id
