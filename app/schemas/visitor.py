"""Schemas for public visitor identity bootstrap flows."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.db.models.conversation import MessageRole, SessionStatus
from app.db.models.visitor import VisitorSource
from app.schemas.explore import SourceMeta, TenantCategory


class PublicBusinessProfileResponse(BaseModel):
    """Public-facing tenant profile used by the hosted support page."""

    tenant_id: UUID
    public_slug: str
    display_name: str
    description: str | None = None
    long_description: str | None = None
    support_headline: str | None = None
    support_subheadline: str | None = None
    support_hours: str | None = None
    support_response_time: str | None = None
    support_footer_note: str | None = None
    widget_primary_color: str | None = None
    widget_accent_color: str | None = None
    widget_logo_url: str | None = None
    widget_greeting_message: str | None = None
    collect_name_before_chat: bool = False
    collect_email_before_chat: bool = False


class PublicBusinessDirectoryItem(BaseModel):
    """Public business listing item shown in the support directory."""

    tenant_id: UUID
    public_slug: str
    display_name: str
    description: str | None = None
    support_headline: str | None = None
    support_subheadline: str | None = None
    widget_logo_url: str | None = None
    is_featured: bool = False
    category: TenantCategory | None = None
    created_at: datetime


class PublicBusinessDirectoryResponse(BaseModel):
    """Public business directory payload."""

    businesses: list[PublicBusinessDirectoryItem] = Field(default_factory=list)
    total: int
    query: str | None = None
    page: int = 1
    page_size: int = 24
    total_pages: int = 0


class VisitorBootstrapRequest(BaseModel):
    """Bootstrap or resolve a visitor and visitor session for public chat entry."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID | None = None
    workspace_slug: str | None = Field(None, min_length=3, max_length=50)
    visitor_token: str | None = Field(None, min_length=8, max_length=128)
    session_token: str | None = Field(None, min_length=8, max_length=128)
    name: str | None = Field(None, min_length=2, max_length=255)
    email: EmailStr | None = None
    entry_point: VisitorSource = VisitorSource.WIDGET
    metadata: dict[str, Any] | None = None
    # Accepted here to avoid 422 on clients that send them; bootstrap doesn't create
    # a ConversationSession, so these are stored only when a session is later created
    # via the chat route. Forward-compat — keep until clients are guaranteed not to send.
    source: str | None = None
    source_meta: SourceMeta | None = None

    @model_validator(mode="after")
    def validate_workspace_reference(self) -> "VisitorBootstrapRequest":
        """Require one tenant locator."""
        if not self.workspace_id and not self.workspace_slug:
            raise ValueError("workspace_id or workspace_slug is required")
        return self


class VisitorProfileSummary(BaseModel):
    """Safe visitor summary returned to public clients."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    is_blocked: bool
    first_seen_at: datetime
    last_seen_at: datetime


class VisitorIdentitySummary(BaseModel):
    """Internal support-facing summary of a visitor attached to a conversation."""

    id: UUID
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    public_token: str
    first_seen_at: datetime
    last_seen_at: datetime
    is_blocked: bool


class VisitorConversationMessageSummary(BaseModel):
    """Public-safe message transcript summary for a visitor's own session."""

    id: UUID
    role: MessageRole
    content: str
    created_at: datetime
    meta: dict[str, Any] | None = None


class VisitorConversationSummary(BaseModel):
    """Latest visitor-linked conversation returned at bootstrap time."""

    session_id: UUID
    status: SessionStatus
    last_message_at: datetime
    messages: list[VisitorConversationMessageSummary] = Field(default_factory=list)


class VisitorBootstrapResponse(BaseModel):
    """Bootstrap response returned to hosted support clients."""

    tenant: PublicBusinessProfileResponse
    visitor_token: str
    session_token: str
    visitor: VisitorProfileSummary
    last_conversation: VisitorConversationSummary | None = None


class VisitorEnrichmentUpdate(BaseModel):
    """Optional visitor contact fields captured during support flows."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=50)
