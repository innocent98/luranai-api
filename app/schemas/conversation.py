"""Conversation Pydantic schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models.conversation import MessageRole, SessionStatus
from app.schemas.explore import SourceMeta
from app.schemas.presence import CustomerPresence
from app.schemas.rating import SessionRatingBrief
from app.schemas.reply_to import ReplyToOut
from app.schemas.visitor import VisitorIdentitySummary

MAX_ATTACHMENTS_PER_MESSAGE = 3


# Base schemas
class ConversationSessionBase(BaseModel):
    """Base conversation session schema."""

    user_identifier: str = Field(..., min_length=1, max_length=255)
    meta: dict | None = Field(None, description="Additional context metadata")


class LeadCapturedMeta(BaseModel):
    """Schema for lead_captured metadata."""

    name: str | None = None
    email: str


class MessageBase(BaseModel):
    """Base message schema."""

    role: MessageRole
    content: str = Field(..., min_length=1, description="Message content")


# Create schemas
class ConversationSessionCreate(ConversationSessionBase):
    """Schema for creating a conversation session."""

    tenant_id: UUID
    visitor_id: UUID | None = None
    visitor_session_id: UUID | None = None
    customer_user_id: UUID | None = None
    source: str | None = None
    source_meta: SourceMeta | None = None


class MessageCreate(MessageBase):
    """Schema for creating a message."""

    session_id: UUID


# Request schemas
class SessionCreateRequest(BaseModel):
    """Schema for creating a new conversation session."""

    tenant_id: UUID = Field(..., description="Tenant ID")
    user_identifier: str = Field(..., min_length=1, max_length=255, description="User identifier")
    metadata: dict | None = Field(None, description="Optional session metadata")


class MessageRequest(BaseModel):
    """Schema for sending a message (user-facing API)."""

    message: str | None = Field(
        None, max_length=10000, description="User message (optional when attachments are sent)"
    )
    attachment_ids: list[UUID] | None = Field(
        None,
        max_length=MAX_ATTACHMENTS_PER_MESSAGE,
        description="IDs of previously uploaded image attachments to link to this message",
    )
    reply_to_message_id: UUID | None = Field(
        None, description="Message in this session being replied to (requires session_id)"
    )
    session_id: UUID | None = Field(None, description="Existing session ID or null for new")
    tenant_id: UUID | None = Field(None, description="Tenant ID (required for new sessions)")
    user_identifier: str | None = Field(
        None, description="User identifier (required for new sessions)"
    )
    metadata: dict | None = Field(None, description="Optional metadata for new sessions")

    @model_validator(mode="after")
    def _require_text_or_attachment(self) -> "MessageRequest":
        if not (self.message and self.message.strip()) and not self.attachment_ids:
            raise ValueError("Either a non-empty message or at least one attachment is required")
        return self


# Update schemas
class ConversationSessionUpdate(BaseModel):
    """Schema for updating a conversation session."""

    model_config = ConfigDict(extra="forbid")

    status: SessionStatus | None = None
    escalation_reason: str | None = None
    meta: dict | None = None


# Response schemas
class AttachmentOut(BaseModel):
    """Chat attachment surfaced on a message (image or video; url is null once expired)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    url: str | None = None
    content_type: str
    width: int | None = None
    height: int | None = None
    kind: str = "image"
    duration_ms: int | None = None
    poster_url: str | None = None
    expired: bool = False

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, data):  # type: ignore[no-untyped-def]
        # ORM rows: derive the wire kind/expired from the enums.
        if hasattr(data, "status") and hasattr(data, "kind"):
            status = getattr(data.status, "value", data.status)
            kind = getattr(data.kind, "value", data.kind)
            return {
                "id": data.id,
                "url": data.url,
                "content_type": data.content_type,
                "width": data.width,
                "height": data.height,
                "kind": kind,
                "duration_ms": data.duration_ms,
                "poster_url": data.poster_url,
                "expired": status == "expired",
            }
        return data


class MessageResponse(MessageBase):
    """Schema for message response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    retrieved_chunk_ids: list[UUID] | None = None
    token_count: int | None = None
    attachments: list[AttachmentOut] = Field(default_factory=list)
    reply_to: ReplyToOut | None = Field(
        None, description="Quoted message when this is a reply; null (never absent) otherwise"
    )
    meta: dict | None = Field(
        None,
        description=(
            "Additional metadata; may include: "
            "'lead_captured' (LeadCapturedMeta shape) when email first captured in sales mode; "
            "'offer_meeting' (bool) when the agent offers the inline meeting picker; "
            "'quote' ({currency: str, line_items: list, total: int}) when the agent generates "
            "a pricing quotation — only present when quotation_enabled=True on the tenant config "
            "AND the model signals offer_quote AND a matching pricing tier exists."
        ),
    )
    created_at: datetime


class _SessionFields(BaseModel):
    """Session fields shared by the customer-facing and staff-facing responses."""

    model_config = ConfigDict(from_attributes=True)

    meta: dict | None = Field(None, description="Additional context metadata")
    id: UUID
    tenant_id: UUID
    visitor_id: UUID | None = None
    visitor_session_id: UUID | None = None
    customer_user_id: UUID | None = None
    visitor: VisitorIdentitySummary | None = None
    status: SessionStatus
    escalation_reason: str | None = None
    escalated_at: datetime | None = None
    agent_id: UUID | None = None
    agent_joined_at: datetime | None = None
    last_message_at: datetime
    created_at: datetime
    updated_at: datetime


class _SessionDetailFields(BaseModel):
    messages: list[MessageResponse] = Field(default_factory=list)
    ticket_id: UUID | None = None  # latest ticket for this session
    ratings: list[SessionRatingBrief] = Field(
        default_factory=list,
        description="Every rating for the session (AI and human), oldest first; staff detail only",
    )


class SessionResponse(_SessionFields):
    """Customer-facing session (public create, customer history). Carries user_identifier."""

    user_identifier: str = Field(..., min_length=1, max_length=255)


class SessionDetailResponse(SessionResponse, _SessionDetailFields):
    """Schema for session with messages."""


class StaffSessionResponse(_SessionFields):
    """Staff-facing session: no user_identifier (customer socket credential), meta redacted."""

    customer_label: str = Field(..., description="Non-secret display label for the customer")
    customer_presence: CustomerPresence | None = Field(
        None, description="Live customer presence; null when presence is unavailable"
    )


class StaffSessionDetailResponse(StaffSessionResponse, _SessionDetailFields):
    """Staff-facing session with messages."""


class ChatResponse(BaseModel):
    """Schema for chat API response."""

    session_id: UUID
    message: MessageResponse
    user_message_attachments: list[AttachmentOut] = Field(
        default_factory=list,
        description="Image attachments linked to the customer's message in this turn",
    )
    should_escalate: bool = Field(
        default=False, description="Whether this should escalate to human"
    )
    escalation_reason: str | None = Field(None, description="Why escalation is recommended")
    ticket_id: UUID | None = Field(None, description="Ticket ID if escalated")
