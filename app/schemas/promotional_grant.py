"""Schemas for promotional grant management."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

GrantedTier = Literal["lite", "growth", "professional"]
GrantStatus = Literal["active", "expired", "revoked", "converted"]
GrantSource = Literal["admin_bulk", "admin_single", "auto_signup"]


# ---------- Filters & preview ----------


class CohortFilter(BaseModel):
    subscription_statuses: list[str] = Field(
        default_factory=lambda: ["trial_expired", "canceled"],
        max_length=10,
    )
    churned_within_days: int | None = Field(default=90, ge=1, le=3650)


class PreviewRequest(BaseModel):
    filters: CohortFilter
    exclude_already_granted: bool = True


class PreviewTenantSummary(BaseModel):
    tenant_id: UUID
    name: str
    display_name: str
    subscription_status: str
    churned_at: datetime | None = None


class PreviewResponse(BaseModel):
    matched_count: int
    sample_tenants: list[PreviewTenantSummary]
    preview_token: str


# ---------- Bulk create ----------

CAMPAIGN_CODE_REGEX = r"^[A-Z][A-Z0-9_]{2,63}$"


class BulkGrantRequest(BaseModel):
    preview_token: str = Field(..., min_length=10)
    campaign_code: str = Field(..., pattern=CAMPAIGN_CODE_REGEX)
    campaign_name: str | None = Field(default=None, max_length=160)
    granted_tier: GrantedTier
    duration_days: int = Field(..., ge=14, le=180)
    force_replace_active: bool = False
    reason: str = Field(..., min_length=10, max_length=500)


class BulkGrantResponse(BaseModel):
    campaign_code: str
    granted: int
    skipped_already_granted: int
    failed: int
    grant_ids_sample: list[UUID]


# ---------- Single grant + responses ----------


class PromotionalGrantResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    tenant_name: str | None = None
    tenant_display_name: str | None = None
    campaign_code: str
    campaign_name: str | None
    granted_tier: GrantedTier
    duration_days: int
    grant_starts_at: datetime
    grant_ends_at: datetime
    status: GrantStatus
    source: GrantSource
    previous_tier: str
    previous_status: str
    expired_at: datetime | None
    revoked_at: datetime | None
    converted_at: datetime | None
    revoke_reason: str | None
    granted_by_admin_name: str | None
    reason: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RevokeGrantRequest(BaseModel):
    reason: str = Field(..., min_length=10, max_length=500)


class RevokeGrantResponse(BaseModel):
    grant_id: UUID
    status: GrantStatus
    tenant_id: UUID
    reverted_to_tier: str
    reverted_to_status: str


# ---------- Campaign summary ----------


class CampaignStatsBySource(BaseModel):
    admin_bulk: int = 0
    admin_single: int = 0
    auto_signup: int = 0


class CampaignStats(BaseModel):
    total: int
    active: int
    converted: int
    expired: int
    revoked: int
    by_source: CampaignStatsBySource


class CampaignSummary(BaseModel):
    campaign_code: str
    campaign_name: str | None
    granted_tier: GrantedTier
    duration_days: int
    first_grant_at: datetime
    stats: CampaignStats


class CampaignListResponse(BaseModel):
    campaigns: list[CampaignSummary]


# ---------- Auto-signup config ----------


class AutoSignupConfigResponse(BaseModel):
    enabled: bool
    campaign_code: str | None
    campaign_name: str | None
    granted_tier: GrantedTier | None
    duration_days: int | None
    valid_until: datetime | None
    updated_at: datetime
    updated_by_admin_name: str | None

    model_config = {"from_attributes": True}


class AutoSignupConfigUpdateRequest(BaseModel):
    enabled: bool
    campaign_code: str | None = Field(default=None, pattern=CAMPAIGN_CODE_REGEX)
    campaign_name: str | None = Field(default=None, max_length=160)
    granted_tier: GrantedTier | None = None
    duration_days: int | None = Field(default=None, ge=14, le=180)
    valid_until: datetime | None = None


class AutoSignupConfigUpdateResponse(AutoSignupConfigResponse):
    pass
