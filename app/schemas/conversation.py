"""Conversation Pydantic schemas."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict

from app.db.models.conversation import SessionStatus, MessageRole


# Base schemas
class ConversationSessionBase(BaseModel):
    """Base conversation session schema."""

    user_identifier: str = Field(..., min_length=1, max_length=255)
    meta: Optional[dict] = Field(None, description="Additional context metadata")


class MessageBase(BaseModel):
    """Base message schema."""

    role: MessageRole
    content: str = Field(..., min_length=1, description="Message content")


# Create schemas
class ConversationSessionCreate(ConversationSessionBase):
    """Schema for creating a conversation session."""

    tenant_id: UUID


class MessageCreate(MessageBase):
    """Schema for creating a message."""

    session_id: UUID


# Request schemas
class SessionCreateRequest(BaseModel):
    """Schema for creating a new conversation session."""

    tenant_id: UUID = Field(..., description="Tenant ID")
    user_identifier: str = Field(..., min_length=1, max_length=255, description="User identifier")
    metadata: Optional[dict] = Field(None, description="Optional session metadata")


class MessageRequest(BaseModel):
    """Schema for sending a message (user-facing API)."""

    message: str = Field(..., min_length=1, max_length=10000, description="User message")
    session_id: Optional[UUID] = Field(None, description="Existing session ID or null for new")
    tenant_id: Optional[UUID] = Field(None, description="Tenant ID (required for new sessions)")
    user_identifier: Optional[str] = Field(None, description="User identifier (required for new sessions)")
    metadata: Optional[dict] = Field(None, description="Optional metadata for new sessions")


# Update schemas
class ConversationSessionUpdate(BaseModel):
    """Schema for updating a conversation session."""

    model_config = ConfigDict(extra="forbid")

    status: Optional[SessionStatus] = None
    escalation_reason: Optional[str] = None
    meta: Optional[dict] = None


# Response schemas
class MessageResponse(MessageBase):
    """Schema for message response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    retrieved_chunk_ids: Optional[list[UUID]] = None
    confidence_score: Optional[float] = None
    token_count: Optional[int] = None
    meta: Optional[dict] = None
    created_at: datetime


class SessionResponse(ConversationSessionBase):
    """Schema for conversation session response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    status: SessionStatus
    escalation_reason: Optional[str] = None
    escalated_at: Optional[datetime] = None
    agent_id: Optional[UUID] = None
    agent_joined_at: Optional[datetime] = None
    confidence_scores: Optional[list[float]] = None
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
    should_escalate: bool = Field(default=False, description="Whether this should escalate to human")
    escalation_reason: Optional[str] = Field(None, description="Why escalation is recommended")
    ticket_id: Optional[UUID] = Field(None, description="Ticket ID if escalated")
