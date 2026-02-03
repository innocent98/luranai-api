"""
Plan Service

Centralized subscription plan management with caching.
Provides single source of truth for pricing and capabilities.
"""

from typing import Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from loguru import logger

from app.db.models.subscription_plan import SubscriptionPlan
from app.utils.enterprise_pricing import EnterprisePricingCalculator


class PlanCache:
    """In-memory cache for subscription plans with TTL."""

    def __init__(self, ttl_minutes: int = 5):
        self.ttl_minutes = ttl_minutes
        self._cache: dict[str, SubscriptionPlan] = {}
        self._cache_time: Optional[datetime] = None

    def is_valid(self) -> bool:
        """Check if cache is still valid."""
        if self._cache_time is None:
            return False
        return datetime.utcnow() - self._cache_time < timedelta(minutes=self.ttl_minutes)

    def get(self, tier: str) -> Optional[SubscriptionPlan]:
        """Get plan from cache."""
        if not self.is_valid():
            return None
        return self._cache.get(tier)

    def get_all(self) -> Optional[list[SubscriptionPlan]]:
        """Get all plans from cache."""
        if not self.is_valid():
            return None
        return list(self._cache.values())

    def set(self, plans: list[SubscriptionPlan]) -> None:
        """Update cache with plans."""
        self._cache = {plan.tier: plan for plan in plans}
        self._cache_time = datetime.utcnow()

    def invalidate(self) -> None:
        """Clear cache."""
        self._cache.clear()
        self._cache_time = None


class PlanService:
    """
    Service for managing subscription plans.

    Provides caching, validation, and backward compatibility with hardcoded constants.
    """

    # Shared cache across all instances
    _cache = PlanCache(ttl_minutes=5)

    def __init__(self, db: Session):
        self.db = db

    def get_all_plans(self, active_only: bool = True) -> list[SubscriptionPlan]:
        """
        Get all subscription plans.

        Args:
            active_only: If True, only return active plans

        Returns:
            List of SubscriptionPlan objects
        """
        # Try cache first
        cached_plans = self._cache.get_all()
        if cached_plans is not None:
            if active_only:
                return [p for p in cached_plans if p.is_active]
            return cached_plans

        # Load from database
        query = self.db.query(SubscriptionPlan).order_by(SubscriptionPlan.sort_order)

        if active_only:
            query = query.filter(SubscriptionPlan.is_active == True)

        plans = query.all()

        # Update cache
        self._cache.set(plans)

        logger.debug(f"Loaded {len(plans)} subscription plans from database")

        return plans

    def get_plan(self, tier: str) -> SubscriptionPlan:
        """
        Get single subscription plan by tier.

        Args:
            tier: Plan tier (trial, lite, professional, enterprise)

        Returns:
            SubscriptionPlan object

        Raises:
            ValueError: If plan not found
        """
        # Try cache first
        cached_plan = self._cache.get(tier)
        if cached_plan is not None:
            return cached_plan

        # Load all plans to populate cache
        self.get_all_plans(active_only=False)

        # Try cache again
        cached_plan = self._cache.get(tier)
        if cached_plan is not None:
            return cached_plan

        # Plan not found
        raise ValueError(f"Subscription plan '{tier}' not found")

    def get_plan_config(self, tier: str) -> dict:
        """
        Get plan capabilities as dict.

        Returns dict matching TIER_CONFIGS structure for backward compatibility.

        Args:
            tier: Plan tier

        Returns:
            Dict with capabilities matching TIER_CONFIGS structure

        Example:
            {
                "can_access_analytics": True,
                "max_knowledge_sources": 10,
                "can_use_url_sources": True,
                "max_knowledge_mb": 50,
                "max_conversations_per_month": 1000,
                "max_team_members": 5,
                "max_response_tokens": 1500,
            }
        """
        plan = self.get_plan(tier)
        return plan.get_capabilities()

    def get_pricing(self, tier: str, billing_cycle: str) -> int:
        """
        Get plan price in kobo.

        Args:
            tier: Plan tier
            billing_cycle: 'monthly' or 'annual'

        Returns:
            Price in kobo

        Raises:
            ValueError: If billing_cycle is invalid
        """
        plan = self.get_plan(tier)

        if billing_cycle == "monthly":
            return plan.monthly_price_kobo
        elif billing_cycle == "annual":
            return plan.annual_price_kobo
        else:
            raise ValueError(f"Invalid billing cycle: {billing_cycle}")

    def update_plan(
        self,
        tier: str,
        monthly_price_kobo: Optional[int] = None,
        annual_price_kobo: Optional[int] = None,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        capabilities: Optional[dict] = None,
        features: Optional[list[str]] = None,
        is_active: Optional[bool] = None,
        sort_order: Optional[int] = None,
    ) -> SubscriptionPlan:
        """
        Update subscription plan.

        Args:
            tier: Plan tier to update
            monthly_price_kobo: New monthly price
            annual_price_kobo: New annual price
            display_name: New display name
            description: New description
            capabilities: Dict with capability updates
            features: New features list
            is_active: Active status
            sort_order: Display order

        Returns:
            Updated SubscriptionPlan

        Raises:
            ValueError: If plan not found or validation fails
        """
        plan = self.get_plan(tier)

        # Update pricing
        if monthly_price_kobo is not None:
            if monthly_price_kobo < 0:
                raise ValueError("Monthly price cannot be negative")
            plan.monthly_price_kobo = monthly_price_kobo

        if annual_price_kobo is not None:
            if annual_price_kobo < 0:
                raise ValueError("Annual price cannot be negative")
            plan.annual_price_kobo = annual_price_kobo

        # Update metadata
        if display_name is not None:
            plan.display_name = display_name

        if description is not None:
            plan.description = description

        if features is not None:
            plan.features = features

        if is_active is not None:
            plan.is_active = is_active

        if sort_order is not None:
            plan.sort_order = sort_order

        # Update capabilities
        if capabilities is not None:
            if "can_access_analytics" in capabilities:
                plan.can_access_analytics = capabilities["can_access_analytics"]

            if "max_knowledge_sources" in capabilities:
                plan.max_knowledge_sources = capabilities["max_knowledge_sources"]

            if "can_use_url_sources" in capabilities:
                plan.can_use_url_sources = capabilities["can_use_url_sources"]

            if "max_knowledge_mb" in capabilities:
                plan.max_knowledge_mb = capabilities["max_knowledge_mb"]

            if "max_conversations_per_month" in capabilities:
                plan.max_conversations_per_month = capabilities["max_conversations_per_month"]

            if "max_team_members" in capabilities:
                plan.max_team_members = capabilities["max_team_members"]

            if "max_response_tokens" in capabilities:
                if capabilities["max_response_tokens"] <= 0:
                    raise ValueError("max_response_tokens must be positive")
                plan.max_response_tokens = capabilities["max_response_tokens"]

        # Save changes
        plan.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(plan)

        # Invalidate cache
        self._cache.invalidate()

        logger.info(f"Updated subscription plan: {tier}")

        return plan

    def validate_tier(self, tier: str) -> bool:
        """
        Check if tier exists.

        Args:
            tier: Plan tier to validate

        Returns:
            True if tier exists, False otherwise
        """
        try:
            self.get_plan(tier)
            return True
        except ValueError:
            return False

    def get_available_tiers(self) -> list[str]:
        """
        Get list of available tier names.

        Returns:
            List of tier strings
        """
        plans = self.get_all_plans(active_only=True)
        return [plan.tier for plan in plans]

    def calculate_enterprise_pricing(self, conversations: int) -> dict:
        """
        Calculate Enterprise tier pricing based on conversation volume.

        Args:
            conversations: Expected monthly conversations

        Returns:
            Pricing calculation result with breakdown and margins
        """
        return EnterprisePricingCalculator.calculate_monthly_cost(conversations)

    def get_enterprise_pricing_tiers(self) -> list[dict]:
        """
        Get all Enterprise volume pricing tiers.

        Returns:
            List of volume pricing tier definitions
        """
        return EnterprisePricingCalculator.get_pricing_tiers()

    def compare_tiers_for_volume(self, conversations: int) -> dict:
        """
        Compare Growth vs Enterprise pricing for given volume.

        Args:
            conversations: Expected monthly conversation volume

        Returns:
            Comparison showing recommended tier and savings
        """
        return EnterprisePricingCalculator.compare_with_fixed_tiers(conversations)
