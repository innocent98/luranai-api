"""
Plan Service

Centralized subscription plan management with caching.
Provides single source of truth for pricing and capabilities.
"""

from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import Boolean
from sqlalchemy.orm import Session

from app.db.models.subscription_plan import SubscriptionPlan
from app.utils.enterprise_pricing import EnterprisePricingCalculator


class PlanCache:
    """In-memory cache for subscription plans with TTL."""

    def __init__(self, ttl_minutes: int = 5):
        self.ttl_minutes = ttl_minutes
        self._cache: dict[str, SubscriptionPlan] = {}
        self._cache_time: datetime | None = None

    def is_valid(self) -> bool:
        """Check if cache is still valid."""
        if self._cache_time is None:
            return False
        return datetime.utcnow() - self._cache_time < timedelta(minutes=self.ttl_minutes)

    def get(self, tier: str) -> SubscriptionPlan | None:
        """Get plan from cache."""
        if not self.is_valid():
            return None
        return self._cache.get(tier)

    def get_all(self) -> list[SubscriptionPlan] | None:
        """Get all plans from cache."""
        if not self.is_valid():
            return None
        return list(self._cache.values())

    def set(self, plans: list[SubscriptionPlan]) -> None:
        """Update cache with plans."""
        self._cache = {str(plan.tier): plan for plan in plans}
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
        # Load directly from database.
        # Avoid returning cached ORM instances across sessions to prevent DetachedInstanceError.
        query = self.db.query(SubscriptionPlan).order_by(SubscriptionPlan.sort_order)

        if active_only:
            query = query.filter(SubscriptionPlan.is_active.is_(True))

        plans = query.all()

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
        plan = self.db.query(SubscriptionPlan).filter(SubscriptionPlan.tier == tier).one_or_none()
        if plan is not None:
            return plan

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
            return int(plan.monthly_price_kobo)
        elif billing_cycle == "annual":
            return int(plan.annual_price_kobo)
        else:
            raise ValueError(f"Invalid billing cycle: {billing_cycle}")

    def update_plan(
        self,
        tier: str,
        monthly_price_kobo: int | None = None,
        annual_price_kobo: int | None = None,
        display_name: str | None = None,
        description: str | None = None,
        capabilities: dict | None = None,
        features: list[str] | None = None,
        is_active: bool | None = None,
        sort_order: int | None = None,
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

        def _set_plan_attr(name: str, value: object) -> None:
            setattr(plan, name, value)

        # Update pricing
        if monthly_price_kobo is not None:
            if monthly_price_kobo < 0:
                raise ValueError("Monthly price cannot be negative")
            _set_plan_attr("monthly_price_kobo", monthly_price_kobo)

        if annual_price_kobo is not None:
            if annual_price_kobo < 0:
                raise ValueError("Annual price cannot be negative")
            _set_plan_attr("annual_price_kobo", annual_price_kobo)

        # Update metadata
        if display_name is not None:
            _set_plan_attr("display_name", display_name)

        if description is not None:
            _set_plan_attr("description", description)

        if features is not None:
            _set_plan_attr("features", features)

        if is_active is not None:
            _set_plan_attr("is_active", is_active)

        if sort_order is not None:
            _set_plan_attr("sort_order", sort_order)

        # Update capabilities (every plan capability column; a new column needs no edit here)
        if capabilities is not None:
            self._apply_capability_updates(plan, capabilities)

        # Save changes
        _set_plan_attr("updated_at", datetime.utcnow())
        self.db.commit()
        self.db.refresh(plan)

        # Invalidate cache
        self._cache.invalidate()

        logger.info(f"Updated subscription plan: {tier}")

        return plan

    @staticmethod
    def _apply_capability_updates(plan: SubscriptionPlan, capabilities: dict) -> None:
        """Validate and set capability columns on a plan."""
        columns = {c.key: c for c in SubscriptionPlan.__table__.columns}
        allowed = set(SubscriptionPlan.capability_columns())
        for name, value in capabilities.items():
            if name not in allowed:
                raise ValueError(f"Unknown plan capability: {name}")
            if isinstance(columns[name].type, Boolean):
                if not isinstance(value, bool):
                    raise ValueError(f"{name} must be true or false")
            elif value is not None:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{name} must be a whole number")
                if name == "max_response_tokens":
                    if value <= 0:
                        raise ValueError("max_response_tokens must be positive")
                elif value < 0:
                    raise ValueError(f"{name} cannot be negative")
            setattr(plan, name, value)

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
        return [str(plan.tier) for plan in plans]

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
