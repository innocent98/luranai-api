"""Customer conversation schemas."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.conversation import SessionDetailResponse


class CustomerConversationSummary(BaseModel):
    """Customer inbox item."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    tenant_id: UUID
    business_name: str
    business_slug: str | None = None
    business_logo_url: str | None = None
    status: str
    last_message_at: str
    latest_message_preview: str | None = None
    has_unread: bool = False


class CustomerConversationDetail(BaseModel):
    """Customer conversation detail with business context."""

    model_config = ConfigDict(extra="forbid")

    business_name: str
    business_slug: str | None = None
    business_logo_url: str | None = None
    business_description: str | None = None
    business_widget_primary_color: str | None = None
    business_widget_accent_color: str | None = None
    business_widget_greeting_message: str | None = None
    conversation: SessionDetailResponse
