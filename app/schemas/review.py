"""Review and rating Pydantic schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, computed_field, field_validator

from app.db.models.review import FeedbackCategory

# ===== Session Rating Schemas =====


class SessionRatingBase(BaseModel):
    """Base session rating schema."""

    score: int = Field(..., ge=1, le=5, description="Rating score 1-5")
    comment: str | None = Field(None, max_length=2000, description="Optional feedback comment")
    meta: dict | None = Field(None, description="Additional context")


class SessionRatingCreate(SessionRatingBase):
    """Schema for creating a session rating (public endpoint).

    Unknown fields (e.g. a client-sent rated_agent_id) are ignored: the rated agent is
    derived server-side from rating_request_token only (N4-D2).
    """

    comment: str | None = Field(None, max_length=500, description="Optional comment (<= 500)")
    session_id: UUID = Field(..., description="Conversation session ID")
    user_identifier: str = Field(
        ..., min_length=1, max_length=255, description="User identifier for verification"
    )
    rating_request_token: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="Token from the rating_requested frame / message meta; rates that agent",
    )


class SessionRatingSubmitResponse(SessionRatingBase):
    """201 body of the public POST (no staff-only fields)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    session_id: UUID
    user_identifier: str
    created_at: datetime
    updated_at: datetime
    rated_agent_id: UUID | None = None
    source: str = "idle_prompt"
    rating_request_id: UUID | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rated_party(self) -> str:
        return "human" if self.rated_agent_id else "ai"


class SessionRatingResponse(SessionRatingBase):
    """Staff list item. No user_identifier (customer socket credential): `customer_label`
    is the non-secret display value. Adds the agent's name and the originating ticket."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    session_id: UUID
    customer_label: str = ""
    created_at: datetime
    updated_at: datetime
    rated_agent_id: UUID | None = None
    source: str = "idle_prompt"
    rating_request_id: UUID | None = None
    rated_agent_name: str | None = None
    ticket_id: UUID | None = None
    ticket_number: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rated_party(self) -> str:
        return "human" if self.rated_agent_id else "ai"


class SessionRatingListResponse(BaseModel):
    """Schema for paginated session ratings list."""

    ratings: list[SessionRatingResponse]
    total: int
    average_score: float | None = Field(None, description="Average rating score")
    csat_percent: float | None = Field(
        None, description="% of filtered ratings scoring >= 4 (1 dp); null when none"
    )
    page: int
    page_size: int


# ===== Platform Feedback Schemas =====


class PlatformFeedbackBase(BaseModel):
    """Base platform feedback schema."""

    category: FeedbackCategory = Field(..., description="Feedback category")
    satisfaction_score: int | None = Field(
        None, ge=1, le=5, description="1-5 rating (for platform_satisfaction)"
    )
    title: str = Field(..., min_length=1, max_length=500, description="Feedback title/summary")
    description: str = Field(
        ..., min_length=1, max_length=10000, description="Detailed feedback description"
    )
    meta: dict | None = Field(None, description="Additional context")

    @field_validator("satisfaction_score")
    @classmethod
    def validate_satisfaction_score(cls, v: int | None, info: ValidationInfo) -> int | None:
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

    status: str | None = Field(None, pattern="^(open|acknowledged|in_progress|resolved|closed)$")
    meta: dict | None = None


class PlatformFeedbackResponse(PlatformFeedbackBase):
    """Schema for platform feedback response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    status: str
    submitted_by: UUID | None = None
    created_at: datetime
    updated_at: datetime


class PlatformFeedbackListResponse(BaseModel):
    """Schema for paginated platform feedback list."""

    feedback: list[PlatformFeedbackResponse]
    total: int
    page: int
    page_size: int


# ===== Public Landing Page Feedback Schemas =====


class PublicFeedbackCreate(BaseModel):
    """Schema for creating public feedback from landing page (no authentication)."""

    category: FeedbackCategory = Field(..., description="Feedback category")
    satisfaction_score: int | None = Field(None, ge=1, le=5, description="1-5 rating (optional)")
    title: str = Field(..., min_length=1, max_length=500, description="Feedback title/summary")
    description: str = Field(
        ..., min_length=1, max_length=10000, description="Detailed feedback description"
    )

    # Optional contact information for follow-up
    name: str | None = Field(None, max_length=255, description="Submitter name (optional)")
    email: str | None = Field(None, max_length=255, description="Contact email (optional)")
    company: str | None = Field(None, max_length=255, description="Company name (optional)")

    meta: dict | None = Field(
        None, description="Additional context (e.g., referral source, page URL)"
    )

    @field_validator("satisfaction_score")
    @classmethod
    def validate_satisfaction_score(cls, v: int | None) -> int | None:
        """Validate satisfaction score range."""
        if v is not None and (v < 1 or v > 5):
            raise ValueError("satisfaction_score must be between 1 and 5")
        return v


class PublicFeedbackResponse(BaseModel):
    """Schema for public feedback response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    category: FeedbackCategory
    satisfaction_score: int | None
    title: str
    description: str
    name: str | None
    email: str | None
    company: str | None
    status: str
    meta: dict | None
    created_at: datetime
    updated_at: datetime


class PublicFeedbackListResponse(BaseModel):
    """Schema for paginated public feedback list."""

    feedback: list[PublicFeedbackResponse]
    total: int
    page: int
    page_size: int
