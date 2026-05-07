from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TenantCategory(str, Enum):
    RETAIL_ECOMMERCE         = "retail_ecommerce"
    SOFTWARE_SAAS            = "software_saas"
    FOOD_BEVERAGE            = "food_beverage"
    HEALTH_WELLNESS          = "health_wellness"
    BEAUTY_PERSONAL_CARE     = "beauty_personal_care"
    EDUCATION_COACHING       = "education_coaching"
    PROFESSIONAL_SERVICES    = "professional_services"
    FINANCIAL_INSURANCE      = "financial_insurance"
    TRAVEL_HOSPITALITY       = "travel_hospitality"
    REAL_ESTATE              = "real_estate"
    MEDIA_ARTS_ENTERTAINMENT = "media_arts_entertainment"
    NONPROFIT                = "nonprofit"
    OTHER                    = "other"


class ConversationSource(str, Enum):
    WIDGET           = "widget"
    EXPLORE_PERSONAL = "explore_personal"
    EXPLORE_BROWSE   = "explore_browse"
    EXPLORE_SEARCH   = "explore_search"
    DIRECT           = "direct"
    UNKNOWN          = "unknown"


class SourceMeta(BaseModel):
    """Optional attribution context written alongside ConversationSource."""

    model_config = ConfigDict(extra="ignore")  # forward-compat: drop unknown keys

    category_filter: Optional[TenantCategory] = None
    search_query_present: Optional[bool] = None
    position: Optional[int] = None
    page: Optional[int] = None


class PersonalBusinessRelationship(BaseModel):
    is_followed: bool
    followed_at: Optional[datetime]
    last_chat_at: Optional[datetime]
    last_activity_at: datetime


class PersonalBusinessItem(BaseModel):
    public_slug: str
    display_name: str
    description: Optional[str]
    support_headline: Optional[str]
    support_subheadline: Optional[str]
    widget_logo_url: Optional[str]
    is_featured: bool
    category: Optional[TenantCategory]
    created_at: datetime
    relationship: PersonalBusinessRelationship


class PersonalBusinessesResponse(BaseModel):
    businesses: list[PersonalBusinessItem]
