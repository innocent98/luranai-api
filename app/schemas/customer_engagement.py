"""Schemas for customer follows and business channel posts."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.customer_engagement import ChannelPostStatus


class CustomerFollowStatusResponse(BaseModel):
    tenant_id: UUID
    is_following: bool
    follower_count: int
    published_post_count: int


class CustomerFollowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tenant_id: UUID
    business_name: str
    business_slug: str | None = None
    business_logo_url: str | None = None
    business_description: str | None = None
    notifications_enabled: bool = True
    followed_at: datetime
    latest_post_title: str | None = None
    latest_post_published_at: datetime | None = None


class CustomerChannelPostSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    business_name: str
    business_slug: str | None = None
    business_logo_url: str | None = None
    title: str
    excerpt: str
    cover_image_url: str | None = None
    cta_label: str | None = None
    cta_url: str | None = None
    is_pinned: bool = False
    is_following: bool = False
    published_at: datetime


class CustomerChannelPostDetail(CustomerChannelPostSummary):
    content: str


class TenantChannelPostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=3, max_length=160)
    excerpt: str = Field(..., min_length=8, max_length=320)
    content: str = Field(..., min_length=20, max_length=20000)
    cover_image_url: str | None = Field(None, max_length=500)
    cta_label: str | None = Field(None, max_length=80)
    cta_url: str | None = Field(None, max_length=1000)
    is_pinned: bool = False
    publish_now: bool = False


class TenantChannelPostUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, min_length=3, max_length=160)
    excerpt: str | None = Field(None, min_length=8, max_length=320)
    content: str | None = Field(None, min_length=20, max_length=20000)
    cover_image_url: str | None = Field(None, max_length=500)
    cta_label: str | None = Field(None, max_length=80)
    cta_url: str | None = Field(None, max_length=1000)
    is_pinned: bool | None = None


class TenantChannelPostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    title: str
    excerpt: str
    content: str
    cover_image_url: str | None = None
    cta_label: str | None = None
    cta_url: str | None = None
    is_pinned: bool
    status: ChannelPostStatus
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TenantChannelOverviewResponse(BaseModel):
    drafts: int
    published: int
    archived: int
    followers: int
