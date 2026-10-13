"""N4 agent-rating schemas (spec §4.3)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SessionRatingBrief(BaseModel):
    """One rating as shown on conversation/ticket detail (staff)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    score: int
    comment: str | None = None
    created_at: datetime
    rated_party: str = "ai"
    rated_agent_id: UUID | None = None
    rated_agent_name: str | None = None
    source: str = "idle_prompt"


class RatingRequestOut(BaseModel):
    """201 body of POST /tickets/{id}/rating-requests. Never carries the token."""

    request_id: UUID
    ticket_id: UUID | None
    session_id: UUID
    agent_id: UUID
    agent_name: str | None
    source: str
    message_id: UUID | None
    created_at: datetime
    expires_at: datetime


class OpenRatingRequest(BaseModel):
    request_id: UUID
    source: str
    created_at: datetime
    expires_at: datetime


class RatingStatusResponse(BaseModel):
    can_request: bool
    reason_code: str | None = None
    retry_after_seconds: int | None = None
    open_request: OpenRatingRequest | None = None
    rating: SessionRatingBrief | None = None


class MyRatingItem(BaseModel):
    id: UUID
    score: int
    comment: str | None = None
    created_at: datetime
    ticket_number: str | None = None
    source: str


class MyRatingsResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    count: int
    average_score: float | None = None
    csat_percent: float | None = None
    recent: list[MyRatingItem] = Field(default_factory=list)
