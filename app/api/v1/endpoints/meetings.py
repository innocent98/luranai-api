"""Public widget endpoints for the inline meeting picker (Stage 3 Slice 3).

Tenant is resolved from the lead (looked up by conversation_id); the picker is
only offered after a lead exists, so the lead is the natural, tenant-scoped anchor.
No authentication required — these are public widget endpoints.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models.lead import Lead
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.schemas.meeting import (
    AvailabilityResponse,
    AvailabilitySlot,
    BookedMeeting,
    BookMeetingRequest,
    BookMeetingResponse,
)
from app.services.meeting_service import MeetingService, SlotUnavailable

router = APIRouter()


def _err(code: str, message: str, http: int) -> JSONResponse:
    return JSONResponse(
        status_code=http,
        content={"success": False, "error": {"code": code, "message": message}},
    )


def _resolve(
    db: Session, conversation_id: UUID
) -> tuple[Lead, TenantSalesAgentConfig] | None:
    """Return (lead, cfg) for this conversation_id, or None if either is missing."""
    lead = (
        db.execute(select(Lead).where(Lead.conversation_id == conversation_id))
        .scalars()
        .first()
    )
    if lead is None or lead.tenant_id is None:
        return None
    cfg = db.execute(
        select(TenantSalesAgentConfig).where(
            TenantSalesAgentConfig.tenant_id == lead.tenant_id
        )
    ).scalar_one_or_none()
    return (lead, cfg) if cfg is not None else None


@router.get("/meetings/availability")
def get_availability(
    conversation_id: UUID = Query(...),
    days: int = Query(default=14, ge=1, le=60),
    db: Session = Depends(get_db),
):
    """Return available booking slots for the tenant owning this conversation.

    Returns an empty slot list (not an error) when the conversation has no lead
    or when meeting booking is disabled — the widget should simply hide the picker.
    """
    resolved = _resolve(db, conversation_id)
    if resolved is None:
        return {"success": True, "data": {"slots": []}}
    lead, cfg = resolved
    if not cfg.meeting_booking_enabled:
        return {"success": True, "data": {"slots": []}}
    slots = MeetingService(db).compute_availability(
        lead.tenant_id, cfg.availability, now=datetime.now(timezone.utc), days=days
    )
    payload = AvailabilityResponse(
        slots=[AvailabilitySlot(start_utc=s) for s in slots]
    )
    return {"success": True, "data": payload.model_dump()}


@router.post("/meetings")
def book_meeting(payload: BookMeetingRequest, db: Session = Depends(get_db)):
    """Book a meeting slot for the lead identified by conversation_id.

    - 404 if no lead exists for this conversation.
    - 403 if meeting booking is disabled for this tenant.
    - 409 if the requested slot is already taken or in the past.
    """
    resolved = _resolve(db, payload.conversation_id)
    if resolved is None:
        return _err("LEAD_NOT_FOUND", "No lead for this conversation", 404)
    lead, cfg = resolved
    if not cfg.meeting_booking_enabled:
        return _err("BOOKING_DISABLED", "Meeting booking is not enabled", 403)
    try:
        meeting = MeetingService(db).book(
            lead.tenant_id,
            lead,
            scheduled_at=payload.scheduled_at,
            visitor_timezone=payload.visitor_timezone,
            use_case=payload.use_case,
            duration_min=int(cfg.availability.get("slot_minutes", 30)),
        )
    except SlotUnavailable as exc:
        return _err("SLOT_UNAVAILABLE", str(exc), 409)
    resp = BookMeetingResponse(
        meeting=BookedMeeting(
            scheduled_at=meeting.scheduled_at,
            duration_min=meeting.duration_min,
        )
    )
    return {"success": True, "data": resp.model_dump()}
