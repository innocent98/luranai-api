"""Schemas for the customer-facing explore surface (categories + attribution)."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TenantCategory(str, Enum):
    RETAIL_ECOMMERCE = "retail_ecommerce"
    SOFTWARE_SAAS = "software_saas"
    FOOD_BEVERAGE = "food_beverage"
    HEALTH_WELLNESS = "health_wellness"
    BEAUTY_PERSONAL_CARE = "beauty_personal_care"
    EDUCATION_COACHING = "education_coaching"
    PROFESSIONAL_SERVICES = "professional_services"
    FINANCIAL_INSURANCE = "financial_insurance"
    TRAVEL_HOSPITALITY = "travel_hospitality"
    REAL_ESTATE = "real_estate"
    MEDIA_ARTS_ENTERTAINMENT = "media_arts_entertainment"
    NONPROFIT = "nonprofit"
    OTHER = "other"


class ConversationSource(str, Enum):
    WIDGET = "widget"
    EXPLORE_PERSONAL = "explore_personal"
    EXPLORE_BROWSE = "explore_browse"
    EXPLORE_SEARCH = "explore_search"
    DIRECT = "direct"
    WHATSAPP = "whatsapp"
    UNKNOWN = "unknown"


class SourceMeta(BaseModel):
    """Optional attribution context written alongside ConversationSource."""

    model_config = ConfigDict(extra="ignore")  # forward-compat: drop unknown keys

    category_filter: TenantCategory | None = None
    search_query_present: bool | None = None
    position: int | None = None
    page: int | None = None
    channel_connection_id: str | None = None
    provider: str | None = None


class PersonalBusinessRelationship(BaseModel):
    is_followed: bool
    followed_at: datetime | None = None
    last_chat_at: datetime | None = None
    last_activity_at: datetime


class PersonalBusinessItem(BaseModel):
    public_slug: str
    display_name: str
    description: str | None = None
    support_headline: str | None = None
    support_subheadline: str | None = None
    widget_logo_url: str | None = None
    is_featured: bool
    category: TenantCategory | None = None
    created_at: datetime
    relationship: PersonalBusinessRelationship


class PersonalBusinessesResponse(BaseModel):
    businesses: list[PersonalBusinessItem] = Field(default_factory=list)
