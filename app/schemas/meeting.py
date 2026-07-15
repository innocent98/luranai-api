"""Pydantic schemas for the meeting booking widget endpoints (Stage 3 Slice 3)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AvailabilitySlot(BaseModel):
    start_utc: datetime


class AvailabilityResponse(BaseModel):
    slots: list[AvailabilitySlot]


class BookMeetingRequest(BaseModel):
    conversation_id: UUID
    scheduled_at: datetime
    visitor_timezone: str | None = None
    use_case: str | None = None


class BookedMeeting(BaseModel):
    scheduled_at: datetime
    duration_min: int


class BookMeetingResponse(BaseModel):
    meeting: BookedMeeting
