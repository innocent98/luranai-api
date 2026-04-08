"""Schemas for platform-admin email campaigns."""

from datetime import UTC, datetime
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

CampaignAudienceType = Literal[
    "all_tenant_admins",
    "active_paid_tenant_admins",
    "trial_tenant_admins",
    "trial_expired_tenant_admins",
    "selected_users",
]
CampaignTemplateKey = Literal[
    "announcement",
    "feature_update",
    "general",
    "maintenance",
    "security",
    "billing",
    "onboarding",
]


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).replace("&nbsp;", " ").strip()


def _contains_meaningful_media(value: str) -> bool:
    return bool(re.search(r"<img\b[^>]*\bsrc\s*=\s*['\"][^'\"]+['\"]", value, flags=re.IGNORECASE))


class PlatformEmailCampaignCreateRequest(BaseModel):
    """Create and queue a platform email campaign."""

    title: str = Field(..., min_length=3, max_length=255)
    audience_type: CampaignAudienceType
    template_key: CampaignTemplateKey
    subject: str = Field(..., min_length=3, max_length=500)
    preheader: str | None = Field(None, max_length=255)
    heading: str = Field(..., min_length=3, max_length=255)
    body: str = Field(..., min_length=10, max_length=15000)
    cta_label: str | None = Field(None, max_length=120)
    cta_url: str | None = Field(None, max_length=1000)
    scheduled_at: datetime | None = None
    selected_user_ids: list[UUID] = Field(default_factory=list)

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or (
            len(_strip_html(cleaned)) < 3 and not _contains_meaningful_media(cleaned)
        ):
            raise ValueError("Body is required")
        return cleaned

    @field_validator("cta_url")
    @classmethod
    def normalize_cta_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("cta_label")
    @classmethod
    def normalize_cta_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("scheduled_at")
    @classmethod
    def normalize_scheduled_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class PlatformEmailCampaignTestSendRequest(BaseModel):
    """Send a test email to the current admin."""

    template_key: CampaignTemplateKey
    subject: str = Field(..., min_length=3, max_length=500)
    preheader: str | None = Field(None, max_length=255)
    heading: str = Field(..., min_length=3, max_length=255)
    body: str = Field(..., min_length=10, max_length=15000)
    cta_label: str | None = Field(None, max_length=120)
    cta_url: str | None = Field(None, max_length=1000)

    @field_validator("body")
    @classmethod
    def normalize_test_body(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or (
            len(_strip_html(cleaned)) < 3 and not _contains_meaningful_media(cleaned)
        ):
            raise ValueError("Body is required")
        return cleaned

    @field_validator("cta_url")
    @classmethod
    def normalize_test_cta_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("cta_label")
    @classmethod
    def normalize_test_cta_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class PlatformEmailCampaignTestSendResponse(BaseModel):
    success: bool
    message: str


class PlatformEmailImageUploadResponse(BaseModel):
    message: str
    image_url: str


class PlatformEmailRecipientResponse(BaseModel):
    """Recipient status for a campaign."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None = None
    user_id: UUID | None = None
    recipient_email: EmailStr
    recipient_name: str | None = None
    tenant_name: str | None = None
    status: str
    sent_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime


class PlatformEmailCampaignSummaryResponse(BaseModel):
    """Summary response for list/create operations."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_by_admin_id: UUID
    created_by_admin_name: str
    title: str
    audience_type: str
    template_key: str
    status: str
    subject: str
    heading: str
    total_recipients: int
    success_count: int
    failure_count: int
    queued_at: datetime | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    sent_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime


class PlatformEmailCampaignDetailResponse(PlatformEmailCampaignSummaryResponse):
    """Detailed campaign response with recipient breakdown."""

    preheader: str | None = None
    body: str
    cta_label: str | None = None
    cta_url: str | None = None
    audience_summary: dict | None = None
    recipients: list[PlatformEmailRecipientResponse]
