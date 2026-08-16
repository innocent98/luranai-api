"""Request/response models for the Agent Caller mobile app (`/api/v1/agent`)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.agent_device import DevicePlatform
from app.db.models.user import AvailabilityStatus, UserRole


class DeviceRegisterRequest(BaseModel):
    """Upsert this device's FCM registration token."""

    model_config = ConfigDict(extra="forbid")

    device_token: str = Field(..., min_length=1, max_length=4096)
    platform: DevicePlatform


class DeviceUnregisterRequest(BaseModel):
    """Remove a device on logout or token rotation."""

    model_config = ConfigDict(extra="forbid")

    device_token: str = Field(..., min_length=1, max_length=4096)


class AvailabilityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AvailabilityStatus


class AvailabilityResponse(BaseModel):
    status: AvailabilityStatus


class AgentMeResponse(BaseModel):
    """Contract addendum A1 — identity + tenant name + availability in one call."""

    user_id: UUID
    email: str
    full_name: str
    role: UserRole
    tenant_id: UUID
    tenant_name: str
    availability_status: AvailabilityStatus


class EscalationSummary(BaseModel):
    """One row of `GET /agent/escalations` (contract §4.2)."""

    escalation_id: UUID
    session_id: UUID
    customer_name: str
    reason: str
    priority: str
    status: str
    created_at: datetime


class TranscriptMessage(BaseModel):
    role: str
    content: str
    created_at: datetime


class EscalationDetail(EscalationSummary):
    """Detail view: summary fields plus the conversation transcript."""

    handled_by: str | None = None
    acknowledged_at: datetime | None = None
    transcript: list[TranscriptMessage]


class AcknowledgeWon(BaseModel):
    won: bool = True
    session_id: UUID
    ws_url: str


class AcknowledgeLost(BaseModel):
    won: bool = False
    handled_by: str
