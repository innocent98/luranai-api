"""Ticket Pydantic schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.conversation import TicketPriority, TicketStatus
from app.schemas.visitor import VisitorIdentitySummary


# Nested schemas
class AssignedAgent(BaseModel):
    """Schema for assigned agent information."""

    id: UUID
    email: str
    full_name: str
    role: str


# Base schemas
class TicketBase(BaseModel):
    """Base ticket schema."""

    summary: str = Field(..., min_length=1, description="Issue summary")
    priority: TicketPriority = Field(default=TicketPriority.MEDIUM, description="Ticket priority")


# Create schemas
class TicketCreate(TicketBase):
    """Schema for creating a ticket."""

    tenant_id: UUID
    session_id: UUID
    priority: TicketPriority = Field(default=TicketPriority.MEDIUM)


class TicketManualCreate(TicketBase):
    """Schema for manually creating a ticket (with all fields)."""

    session_id: UUID
    priority: TicketPriority = Field(default=TicketPriority.MEDIUM)


# Update schemas
class TicketUpdate(BaseModel):
    """Schema for updating a ticket."""

    model_config = ConfigDict(extra="forbid")

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assigned_to: UUID | None = None
    resolution_notes: str | None = None


class TicketAssign(BaseModel):
    """Schema for assigning a ticket."""

    assigned_to: UUID = Field(..., description="User ID to assign ticket to")


class TicketResolve(BaseModel):
    """Schema for resolving a ticket."""

    resolution_notes: str = Field(..., min_length=1, description="Resolution notes")


# Response schemas
class TicketResponse(TicketBase):
    """Schema for ticket response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    session_id: UUID
    ticket_number: str
    status: TicketStatus
    assigned_to: UUID | None = None  # Kept for backwards compatibility
    assigned_agent: AssignedAgent | None = None  # New: full agent details
    visitor: VisitorIdentitySummary | None = None
    resolution_notes: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TicketListResponse(BaseModel):
    """Schema for ticket list response."""

    tickets: list[TicketResponse]
    total: int
    page: int
    page_size: int
