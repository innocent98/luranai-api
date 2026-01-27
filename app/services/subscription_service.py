"""Manage tenant subscriptions and tier changes."""

from datetime import datetime, timezone
from uuid import UUID
from sqlalchemy.orm import Session
from app.db.models.tenant import Tenant, TenantCapabilities


class SubscriptionService:
    """Manage tenant subscriptions and tier changes."""

    # Tier configurations
    TIER_CONFIGS = {
        "trial": {
            "can_access_analytics": False,
            "max_knowledge_sources": 2,
            "can_use_url_sources": False,
            "max_knowledge_mb": 10,
            "max_conversations_per_month": 100,
            "max_team_members": 1,
            "max_response_tokens": 800,
        },
        "lite": {
            "can_access_analytics": True,
            "max_knowledge_sources": 10,
            "can_use_url_sources": True,
            "max_knowledge_mb": 50,
            "max_conversations_per_month": 1000,
            "max_team_members": 3,
            "max_response_tokens": 1000,
        },
        "professional": {
            "can_access_analytics": True,
            "max_knowledge_sources": 30,
            "can_use_url_sources": True,
            "max_knowledge_mb": 200,
            "max_conversations_per_month": 5000,
            "max_team_members": 10,
            "max_response_tokens": 1500,
        },
        "enterprise": {
            "can_access_analytics": True,
            "max_knowledge_sources": None,  # Unlimited
            "can_use_url_sources": True,
            "max_knowledge_mb": None,       # Unlimited
            "max_conversations_per_month": None,
            "max_team_members": None,
            "max_response_tokens": 2000,
        },
    }

    def __init__(self, db: Session):
        """Initialize subscription service with database session."""
        self.db = db

    def upgrade_subscription(
        self,
        tenant_id: UUID,
        tier: str,
        billing_cycle: str = "monthly"
    ) -> Tenant:
        """
        Upgrade tenant to paid tier.

        Args:
            tenant_id: Tenant UUID
            tier: New tier (lite, professional, enterprise)
            billing_cycle: monthly or annual

        Returns:
            Updated tenant

        Raises:
            ValueError: If tenant not found or invalid tier
        """
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        if tier not in self.TIER_CONFIGS:
            raise ValueError(f"Invalid tier: {tier}")

        # Update tenant subscription
        tenant.subscription_tier = tier
        tenant.subscription_status = "active"
        tenant.billing_cycle = billing_cycle
        tenant.subscription_starts_at = datetime.now(timezone.utc)
        tenant.trial_ends_at = None  # Clear trial

        # Update capabilities based on tier
        self._update_capabilities_for_tier(tenant_id, tier)

        self.db.commit()
        self.db.refresh(tenant)

        return tenant

    def _update_capabilities_for_tier(self, tenant_id: UUID, tier: str):
        """
        Update tenant capabilities based on tier (respecting manual overrides).

        Manual capability overrides tracked in override_flags will be preserved
        during tier upgrades. This allows platform admins to grant custom limits
        that persist through subscription changes.
        """
        config = self.TIER_CONFIGS.get(tier)
        if not config:
            raise ValueError(f"Invalid tier: {tier}")

        capabilities = self.db.query(TenantCapabilities).filter(
            TenantCapabilities.tenant_id == tenant_id
        ).first()

        if not capabilities:
            raise ValueError("Capabilities not found")

        # Get override flags (fields that should NOT be reset)
        override_flags = capabilities.override_flags or {}

        # Update capability fields, skipping manually overridden ones
        for key, value in config.items():
            if hasattr(capabilities, key):
                # Skip if manually overridden by platform admin
                if key not in override_flags:
                    setattr(capabilities, key, value)
                # else: keep current value (override persists)

        self.db.commit()
