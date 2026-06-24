"""Pydantic v2 schemas for the Sales Config API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class SalesConfig(BaseModel):
    """Full read mirror of TenantSalesAgentConfig."""

    id: uuid.UUID | None = None
    tenant_id: uuid.UUID

    # Feature toggles
    agent_enabled: bool
    lead_capture_enabled: bool
    meeting_booking_enabled: bool
    quotation_enabled: bool

    # Integration providers
    crm_provider: str | None = None
    calendar_provider: str | None = None

    # Notification settings
    notify_channels: dict
    notification_email: str | None = None

    # Agent behaviour
    trigger_intents: list
    qualification_questions: list
    pricing_tiers: list
    currency: str

    # Timestamps (None when serializing an unpersisted default)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SalesConfigPatch(BaseModel):
    """All-optional patch body for PATCH /sales/config."""

    agent_enabled: bool | None = None
    lead_capture_enabled: bool | None = None
    meeting_booking_enabled: bool | None = None
    quotation_enabled: bool | None = None

    crm_provider: str | None = None
    calendar_provider: str | None = None

    notify_channels: dict | None = None
    notification_email: str | None = None

    trigger_intents: list | None = None
    qualification_questions: list | None = None
    pricing_tiers: list | None = None
    currency: str | None = None
