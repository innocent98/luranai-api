"""Authenticated tenant-admin endpoints for the Leads pipeline."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.db.models.lead import LeadStatus
from app.sales.scoring import tier_for
from app.schemas.lead import (
    LeadDetail,
    LeadListItem,
    LeadListResponse,
    LeadStatusPatch,
    LeadsSummary,
)
from app.schemas.user import UserResponse
from app.services.lead_service import (
    IllegalStatusTransition,
    LeadNotFound,
    LeadService,
    SortKey,
)

router = APIRouter()


def _err(code: str, message: str, http: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=http,
        content={
            "success": False,
            "error": {"code": code, "message": message, "details": details or {}},
        },
    )


@router.get("")
def list_leads(
    status: LeadStatus | None = Query(default=None),
    intent: str | None = Query(default=None),
    tier: str | None = Query(default=None),
    sort: SortKey = Query(default="score_desc"),
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


# ---------------------------------------------------------------------------
# GET /leads/{lead_id} — lead detail
# ---------------------------------------------------------------------------


@router.get("/{lead_id}")
def get_lead(
    lead_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Return full lead detail for one of this tenant's leads.

    Lazy backfill: if lead_score is None (e.g. pre-scoring leads), score is
    computed and persisted on first view.
    """
    svc = LeadService(db)
    lead = svc.get_lead(user.tenant_id, lead_id)
    if lead is None:
        return _err("LEAD_NOT_FOUND", "Lead not found", 404)

    if lead.lead_score is None:
        svc.apply_score(lead)
        db.commit()

    detail = LeadDetail.from_lead(lead, tier_for(lead.lead_score or 0))
    return {"success": True, "data": detail.model_dump()}


# ---------------------------------------------------------------------------
# PATCH /leads/{lead_id} — status update
# ---------------------------------------------------------------------------


@router.patch("/{lead_id}")
def patch_lead_status(
    lead_id: UUID,
    payload: LeadStatusPatch,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Transition this tenant's lead to a new status.

    Illegal transitions (e.g. CONTACTED → NEW) return 422.
    """
    svc = LeadService(db)

    try:
        lead = svc.set_status(user.tenant_id, lead_id, payload.status)
    except LeadNotFound:
        return _err("LEAD_NOT_FOUND", "Lead not found", 404)
    except IllegalStatusTransition as exc:
        return _err("INVALID_STATUS_TRANSITION", str(exc), 422)

    detail = LeadDetail.from_lead(lead, tier_for(lead.lead_score or 0))
    return {"success": True, "data": detail.model_dump()}


# ---------------------------------------------------------------------------
# POST /leads/{lead_id}/score — recompute score
# ---------------------------------------------------------------------------


@router.post("/{lead_id}/score")
def recompute_lead_score(
    lead_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Re-run scoring for a lead and return the updated detail."""
    svc = LeadService(db)

    try:
        lead = svc.recompute(user.tenant_id, lead_id)
    except LeadNotFound:
        return _err("LEAD_NOT_FOUND", "Lead not found", 404)

    detail = LeadDetail.from_lead(lead, tier_for(lead.lead_score or 0))
    return {"success": True, "data": detail.model_dump()}
