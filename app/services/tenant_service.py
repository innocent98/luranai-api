"""Tenant service for managing tenants, AI configs, and capabilities."""

import logging
import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities, TenantStatus
from app.schemas.tenant import (
    TenantAIConfigResponse,
    TenantAIConfigUpdate,
    TenantCapabilitiesResponse,
    TenantCapabilitiesUpdate,
    TenantCreate,
    TenantListResponse,
    TenantResponse,
    TenantUpdate,
)
from app.services.plan_service import PlanService
from app.utils.slugs import (
    PUBLIC_SLUG_MAX_LENGTH,
    PUBLIC_SLUG_MIN_LENGTH,
    is_valid_public_slug,
    normalize_public_slug,
)

logger = logging.getLogger(__name__)


class TenantService:
    """Service for tenant management operations."""

    def __init__(self, db: Session):
        """Initialize tenant service with database session."""
        self.db = db

    def _get_unique_public_slug(
        self, base_value: str, exclude_tenant_id: UUID | None = None
    ) -> str:
        """Generate a globally unique public slug."""
        base_slug = normalize_public_slug(base_value)
        if len(base_slug) < PUBLIC_SLUG_MIN_LENGTH:
            base_slug = "support"

        candidate = base_slug
        suffix = 2
        while True:
            stmt = select(Tenant).where(Tenant.public_slug == candidate)
            if exclude_tenant_id:
                stmt = stmt.where(Tenant.id != exclude_tenant_id)
            existing = self.db.execute(stmt).scalar_one_or_none()
            if not existing:
                return candidate

            suffix_text = f"-{suffix}"
            trimmed = base_slug[: PUBLIC_SLUG_MAX_LENGTH - len(suffix_text)].rstrip("-")
            candidate = f"{trimmed}{suffix_text}"
            suffix += 1

    def _validate_requested_public_slug(
        self, value: str, exclude_tenant_id: UUID | None = None
    ) -> str:
        """Validate and reserve a user-provided public slug."""
        normalized = value.strip().lower()
        if not is_valid_public_slug(normalized):
            raise ValueError(
                "Public slug must be 3-50 characters using lowercase letters, numbers, and hyphens only"
            )

        stmt = select(Tenant).where(Tenant.public_slug == normalized)
        if exclude_tenant_id:
            stmt = stmt.where(Tenant.id != exclude_tenant_id)
        existing = self.db.execute(stmt).scalar_one_or_none()
        if existing:
            raise ValueError(f"Public slug '{normalized}' is already in use")

        return normalized

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
            public_slug=self._get_unique_public_slug(tenant_data.name),
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
            max_response_tokens=450,
            grounding_requirement=True,
            grounding_mode="balanced",
            low_confidence_action="clarify",
            no_retrieval_action="clarify",
            require_email_before_escalation=False,
            require_name_before_escalation=False,
            collect_name_before_chat=False,
            collect_email_before_chat=False,
        )
        self.db.add(ai_config)

        # Create default capabilities (trial tier)
        capabilities = TenantCapabilities(
            tenant_id=db_tenant.id,
            can_create_tickets=True,
            can_access_analytics=False,  # No analytics in trial
            can_upload_knowledge=True,
            max_knowledge_mb=10,  # 10 MB for trial
            max_conversations_per_month=200,  # 200 conversations/month for trial
            max_knowledge_sources=2,  # Only 2 sources for trial
            can_use_url_sources=False,  # Files only, no URLs in trial
            max_team_members=1,  # Only 1 user (admin) for trial
            max_automation_actions=2,  # Starter automation access in trial
            max_automation_executions_per_month=100,  # Starter monthly automation usage in trial
            can_use_ai_chat_automation=True,  # Trial should experience real AI chat automation
            can_use_lookup_chains=False,  # Lookup-first chaining starts on higher plans
            can_use_webhooks=False,  # Direct API/webhook flow nodes start on higher plans
            max_flows=1,  # Allow one flow on trial
            max_webhooks_per_flow=0,  # No webhook nodes on trial
            max_audits_per_month=2,  # 2 dashboard audits/month on trial
            max_audit_pages_per_crawl=20,  # Shallow crawl depth on trial
        )
        self.db.add(capabilities)

        self.db.commit()
        self.db.refresh(db_tenant)

        # Auto-grant on signup, if campaign is active.
        # Failures here must NEVER block tenant creation.
        from app.services.auto_signup_grant_config_service import AutoSignupGrantConfigService
        from app.services.promotional_grant_service import PromotionalGrantService

        try:
            config_svc = AutoSignupGrantConfigService(self.db)
            if config_svc.is_active():
                cfg = config_svc.get()
                PromotionalGrantService(self.db).create_grant(
                    tenant_id=db_tenant.id,
                    campaign_code=cfg.campaign_code,
                    campaign_name=cfg.campaign_name,
                    granted_tier=cfg.granted_tier,
                    duration_days=cfg.duration_days,
                    source="auto_signup",
                    granted_by_admin_id=None,
                    granted_by_admin_name="system",
                    reason=f"Auto-grant on signup for campaign {cfg.campaign_code}",
                )
        except Exception as e:
            logger.error(f"Auto-grant failed for tenant {db_tenant.id}: {e}")
            # Swallow — tenant creation already succeeded; auto-grant is best-effort.

        # Return tenant with the generated API key (only shown once at creation)
        response = TenantResponse.model_validate(db_tenant)
        response.api_key = api_key  # Add the plain API key to response
        return response

    def get_tenant(self, tenant_id: UUID) -> Tenant | None:
        """
        Get raw tenant model by ID.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Tenant model if found, None otherwise
        """
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_tenant_by_id(
        self, tenant_id: UUID, include_relations: bool = True
    ) -> TenantResponse | TenantListResponse | None:
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

    def get_tenant_by_name(self, name: str) -> TenantResponse | None:
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

    def get_tenant_by_public_slug(self, public_slug: str) -> TenantResponse | None:
        """Get tenant by public support slug."""
        stmt = select(Tenant).where(Tenant.public_slug == public_slug.strip().lower())
        tenant = self.db.execute(stmt).scalar_one_or_none()

        if not tenant:
            return None

        return TenantResponse.model_validate(tenant)

    def get_all_tenants(self, status: TenantStatus | None = None) -> list[TenantListResponse]:
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
        if "public_slug" in update_data and update_data["public_slug"] is not None:
            update_data["public_slug"] = self._validate_requested_public_slug(
                str(update_data["public_slug"]),
                exclude_tenant_id=tenant_id,
            )
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

    def get_ai_config(self, tenant_id: UUID) -> TenantAIConfigResponse | None:
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

    def update_ai_config(
        self, tenant_id: UUID, config_data: TenantAIConfigUpdate
    ) -> TenantAIConfigResponse:
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

        collect_name_before_chat = bool(
            update_data.get("collect_name_before_chat", config.collect_name_before_chat)
        )
        collect_email_before_chat = bool(
            update_data.get("collect_email_before_chat", config.collect_email_before_chat)
        )
        if collect_name_before_chat:
            update_data["require_name_before_escalation"] = False
        if collect_email_before_chat:
            update_data["require_email_before_escalation"] = False

        # Enforce per-tier max_response_tokens ceiling from active plan config.
        if "max_response_tokens" in update_data:
            tenant_stmt = select(Tenant).where(Tenant.id == tenant_id)
            tenant = self.db.execute(tenant_stmt).scalar_one_or_none()
            if not tenant:
                raise ValueError("Tenant not found")

            plan_config = PlanService(self.db).get_plan_config(str(tenant.subscription_tier))
            tier_limit = plan_config.get("max_response_tokens")
            requested = update_data["max_response_tokens"]
            if (
                isinstance(tier_limit, int)
                and isinstance(requested, int)
                and requested > tier_limit
            ):
                raise ValueError(
                    f"max_response_tokens exceeds plan limit ({requested} > {tier_limit})"
                )

        for field, value in update_data.items():
            setattr(config, field, value)

        self.db.commit()
        self.db.refresh(config)

        return TenantAIConfigResponse.model_validate(config)

    # Capabilities methods

    def get_capabilities(self, tenant_id: UUID) -> TenantCapabilitiesResponse | None:
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

        # Record each explicitly-set field as a manual override so a later
        # tier sync (SubscriptionService._update_capabilities_for_tier)
        # preserves it instead of silently reverting to the plan default.
        # Merge onto any existing overrides -- a tenant accumulates these
        # across separate admin actions. Assign a new dict rather than
        # mutating in place: SQLAlchemy does not track in-place mutation of
        # a JSON column, so `capabilities.override_flags[field] = True`
        # would not be persisted on commit.
        if update_data:
            merged_overrides = dict(capabilities.override_flags or {})
            merged_overrides.update(dict.fromkeys(update_data, True))
            capabilities.override_flags = merged_overrides

        self.db.commit()
        self.db.refresh(capabilities)

        return TenantCapabilitiesResponse.model_validate(capabilities)

    def verify_api_key(self, name: str, api_key: str) -> UUID | None:
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
