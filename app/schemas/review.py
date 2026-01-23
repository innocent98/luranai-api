"""Review and rating Pydantic schemas."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict, field_validator

from app.db.models.review import FeedbackCategory


# ===== Session Rating Schemas =====

class SessionRatingBase(BaseModel):
    """Base session rating schema."""

    score: int = Field(..., ge=1, le=5, description="Rating score 1-5")
    comment: Optional[str] = Field(None, max_length=2000, description="Optional feedback comment")
    meta: Optional[dict] = Field(None, description="Additional context")


class SessionRatingCreate(SessionRatingBase):
    """Schema for creating a session rating (public endpoint)."""

    session_id: UUID = Field(..., description="Conversation session ID")
    user_identifier: str = Field(..., min_length=1, max_length=255, description="User identifier for verification")


class SessionRatingResponse(SessionRatingBase):
    """Schema for session rating response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    session_id: UUID
    user_identifier: str
    created_at: datetime
    updated_at: datetime


class SessionRatingListResponse(BaseModel):
    """Schema for paginated session ratings list."""

    ratings: list[SessionRatingResponse]
    total: int
    average_score: Optional[float] = Field(None, description="Average rating score")
    page: int
    page_size: int


# ===== Platform Feedback Schemas =====

class PlatformFeedbackBase(BaseModel):
    """Base platform feedback schema."""

    category: FeedbackCategory = Field(..., description="Feedback category")
    satisfaction_score: Optional[int] = Field(None, ge=1, le=5, description="1-5 rating (for platform_satisfaction)")
    title: str = Field(..., min_length=1, max_length=500, description="Feedback title/summary")
    description: str = Field(..., min_length=1, max_length=10000, description="Detailed feedback description")
    meta: Optional[dict] = Field(None, description="Additional context")

    @field_validator("satisfaction_score")
    @classmethod
    def validate_satisfaction_score(cls, v: Optional[int], info) -> Optional[int]:
        """Ensure satisfaction_score is only set for platform_satisfaction category."""
        if v is not None and v < 1 or (v is not None and v > 5):
            raise ValueError("satisfaction_score must be between 1 and 5")
        return v


class PlatformFeedbackCreate(PlatformFeedbackBase):
    """Schema for creating platform feedback."""

    pass


class PlatformFeedbackUpdate(BaseModel):
    """Schema for updating platform feedback (admin only)."""

    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = Field(None, pattern="^(open|acknowledged|in_progress|resolved|closed)$")
    meta: Optional[dict] = None


class PlatformFeedbackResponse(PlatformFeedbackBase):
    """Schema for platform feedback response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    status: str
    submitted_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime


class PlatformFeedbackListResponse(BaseModel):
    """Schema for paginated platform feedback list."""

    feedback: list[PlatformFeedbackResponse]
    total: int
    page: int
    page_size: int
