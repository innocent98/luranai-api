"""Ticket Pydantic schemas."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict

from app.db.models.conversation import TicketStatus, TicketPriority


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

    status: Optional[TicketStatus] = None
    priority: Optional[TicketPriority] = None
    assigned_to: Optional[UUID] = None
    resolution_notes: Optional[str] = None


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
    assigned_to: Optional[UUID] = None
    resolution_notes: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class TicketListResponse(BaseModel):
    """Schema for ticket list response."""

    tickets: list[TicketResponse]
    total: int
    page: int
    page_size: int
