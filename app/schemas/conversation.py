"""Conversation Pydantic schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.conversation import MessageRole, SessionStatus
from app.schemas.explore import SourceMeta
from app.schemas.visitor import VisitorIdentitySummary


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

    message: str = Field(..., min_length=1, max_length=10000, description="User message")
    session_id: UUID | None = Field(None, description="Existing session ID or null for new")
    tenant_id: UUID | None = Field(None, description="Tenant ID (required for new sessions)")
    user_identifier: str | None = Field(
        None, description="User identifier (required for new sessions)"
    )
    metadata: dict | None = Field(None, description="Optional metadata for new sessions")


# Update schemas
class ConversationSessionUpdate(BaseModel):
    """Schema for updating a conversation session."""

    model_config = ConfigDict(extra="forbid")

    status: SessionStatus | None = None
    escalation_reason: str | None = None
    meta: dict | None = None


# Response schemas
class MessageResponse(MessageBase):
    """Schema for message response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    retrieved_chunk_ids: list[UUID] | None = None
    token_count: int | None = None
    meta: dict | None = Field(
        None,
        description="Additional metadata; may include 'lead_captured' (LeadCapturedMeta shape) when email first captured in sales mode; may include 'offer_meeting' (bool) when the agent offers the inline meeting picker",
    )
    created_at: datetime


class SessionResponse(ConversationSessionBase):
    """Schema for conversation session response."""

    model_config = ConfigDict(from_attributes=True)

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


class SessionDetailResponse(SessionResponse):
    """Schema for session with messages."""

    messages: list[MessageResponse] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """Schema for chat API response."""

    session_id: UUID
    message: MessageResponse
    should_escalate: bool = Field(
        default=False, description="Whether this should escalate to human"
    )
    escalation_reason: str | None = Field(None, description="Why escalation is recommended")
    ticket_id: UUID | None = Field(None, description="Ticket ID if escalated")
