"""
Subscription Plan Model

This module defines the SubscriptionPlan model for database-driven pricing configuration.
"""

from sqlalchemy import Column, String, Integer, Boolean, JSON, Index
from app.db.base import Base, UUIDMixin, TimestampMixin


class SubscriptionPlan(Base, UUIDMixin, TimestampMixin):
    """
    Database model for subscription plans with pricing and capabilities.

    Replaces hardcoded PRICING and TIER_CONFIGS constants with database-driven configuration.
    """
    __tablename__ = "subscription_plans"

    # Identification
    tier = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    description = Column(String, nullable=False)

    # Pricing (all prices in kobo)
    monthly_price_kobo = Column(Integer, nullable=False)
    annual_price_kobo = Column(Integer, nullable=False)

    # Capabilities (matching TIER_CONFIGS structure)
    can_access_analytics = Column(Boolean, nullable=False, default=False)
    max_knowledge_sources = Column(Integer, nullable=True)  # None = unlimited
    can_use_url_sources = Column(Boolean, nullable=False, default=False)
    max_knowledge_mb = Column(Integer, nullable=True)  # None = unlimited
    max_conversations_per_month = Column(Integer, nullable=True)  # None = unlimited
    max_team_members = Column(Integer, nullable=True)  # None = unlimited
    max_response_tokens = Column(Integer, nullable=False)

    # Marketing
    features = Column(JSON, nullable=False, default=list)  # List of feature descriptions

    # Status
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    sort_order = Column(Integer, nullable=False, default=0, index=True)

    __table_args__ = (
        Index('ix_subscription_plans_tier', 'tier', unique=True),
        Index('ix_subscription_plans_active_sort', 'is_active', 'sort_order'),
    )

    def to_dict(self) -> dict:
        """Convert plan to dictionary representation."""
        return {
            "id": str(self.id),
            "tier": self.tier,
            "display_name": self.display_name,
            "description": self.description,
            "monthly_price_kobo": self.monthly_price_kobo,
            "annual_price_kobo": self.annual_price_kobo,
            "monthly_price_naira": self.monthly_price_kobo / 100,
            "annual_price_naira": self.annual_price_kobo / 100,
            "capabilities": {
                "can_access_analytics": self.can_access_analytics,
                "max_knowledge_sources": self.max_knowledge_sources,
                "can_use_url_sources": self.can_use_url_sources,
                "max_knowledge_mb": self.max_knowledge_mb,
                "max_conversations_per_month": self.max_conversations_per_month,
                "max_team_members": self.max_team_members,
                "max_response_tokens": self.max_response_tokens,
            },
            "features": self.features,
            "is_active": self.is_active,
            "sort_order": self.sort_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def get_capabilities(self) -> dict:
        """
        Get capabilities dict matching TIER_CONFIGS structure.

        Ensures backward compatibility with existing code.
        """
        return {
            "can_access_analytics": self.can_access_analytics,
            "max_knowledge_sources": self.max_knowledge_sources,
            "can_use_url_sources": self.can_use_url_sources,
            "max_knowledge_mb": self.max_knowledge_mb,
            "max_conversations_per_month": self.max_conversations_per_month,
            "max_team_members": self.max_team_members,
            "max_response_tokens": self.max_response_tokens,
        }
