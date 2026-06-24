"""Authenticated tenant-admin endpoints for the Leads pipeline."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.db.models.lead import LeadStatus
from app.sales.scoring import tier_for
from app.schemas.lead import LeadListItem, LeadListResponse, LeadsSummary
from app.schemas.user import UserResponse
from app.services.lead_service import LeadService

router = APIRouter()


@router.get("")
def list_leads(
    status: LeadStatus | None = Query(default=None),
    intent: str | None = Query(default=None),
    tier: str | None = Query(default=None),
    sort: Literal["score", "captured"] = Query(default="score"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """List this tenant's leads with optional filtering and pagination."""
    svc = LeadService(db)
    rows, total = svc.list_leads(
        user.tenant_id,
        status=status,
        intent=intent,
        tier=tier,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    items = [LeadListItem.from_lead(lead, tier_for(lead.lead_score or 0)) for lead in rows]
    payload = LeadListResponse(items=items, total=total, limit=limit, offset=offset)
    return {"success": True, "data": payload.model_dump()}


@router.get("/summary")
def leads_summary(
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Return summary statistics for this tenant's lead pipeline."""
    svc = LeadService(db)
    raw = svc.summary(user.tenant_id)
    summary = LeadsSummary(**raw)
    return {"success": True, "data": summary.model_dump()}
