"""Authenticated (tenant-admin) Website Auditor endpoints."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.auditor.service import AuditService, RedirectConfirmationRequired
from app.db.models.audit import (
    AuditEntry,
    AuditEntryKind,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
)
from app.schemas.auditor import DashboardAuditRequest, EntryDetailResponse, EntryEditRequest
from app.schemas.user import UserResponse

router = APIRouter()


def _err(code: str, message: str, http: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=http,
        content={
            "success": False,
            "error": {"code": code, "message": message, "details": details or {}},
        },
    )


def _owned_report(db: Session, audit_id: UUID, tenant_id: UUID) -> AuditReport:
    """Return an AuditReport owned by tenant_id, or raise 404."""
    report = (
        db.query(AuditReport)
        .filter(AuditReport.id == audit_id, AuditReport.tenant_id == tenant_id)
        .one_or_none()
    )
    if not report:
        raise HTTPException(status_code=404, detail="Audit not found")
    return report


def _check_monthly_audit_limit(db: Session, tenant_id: UUID) -> None:
    """Enforce TenantCapabilities.max_audits_per_month.

    Counts this tenant's AuditReport rows created in the current calendar month.
    Raises ValueError (→ 403) when the limit is reached, mirroring
    automation_service._check_action_limit.
    """
    from app.db.models.tenant import TenantCapabilities

    cap = (
        db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant_id).one_or_none()
    )
    if cap is None or cap.max_audits_per_month is None:
        # No capabilities row or unlimited — allow.
        return

    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    count = (
        db.query(AuditReport)
        .filter(
            AuditReport.tenant_id == tenant_id,
            AuditReport.created_at >= month_start,
        )
        .count()
    )
    if count >= cap.max_audits_per_month:
        raise ValueError(
            f"Monthly audit limit reached. Your plan allows {cap.max_audits_per_month} "
            "dashboard audits per month. Upgrade to run more."
        )


# ---------------------------------------------------------------------------
# POST /audit — start a dashboard-scope audit
# ---------------------------------------------------------------------------


@router.post("/audit", status_code=status.HTTP_202_ACCEPTED)
async def start_dashboard_audit(
    payload: DashboardAuditRequest,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    try:
        _check_monthly_audit_limit(db, user.tenant_id)
    except ValueError as exc:
        return _err("AUDIT_LIMIT_REACHED", str(exc), status.HTTP_403_FORBIDDEN)

    try:
        report = await AuditService(db).start_audit(
            url=str(payload.url),
            scope=AuditScope.DASHBOARD,
            tenant_id=user.tenant_id,
            created_by=user.id,
            confirmed_target=payload.confirmed_target,
        )
    except RedirectConfirmationRequired as exc:
        return _err(
            "REDIRECT_CONFIRMATION_REQUIRED",
            f"{exc.entered} redirects to {exc.target}. Scan {exc.target}?",
            status.HTTP_409_CONFLICT,
            details={"entered_url": exc.entered, "redirect_target": exc.target},
        )
    except ValueError as exc:
        return _err("INVALID_URL", str(exc), status.HTTP_400_BAD_REQUEST)
    except RuntimeError:
        return _err(
            "ENQUEUE_FAILED",
            "Audit could not be queued. Please try again later.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"success": True, "data": {"audit_report_id": str(report.id)}},
    )


# ---------------------------------------------------------------------------
# GET /audit — list this tenant's audits
# ---------------------------------------------------------------------------


@router.get("/audit")
def list_audits(
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    rows = (
        db.query(AuditReport)
        .filter(AuditReport.tenant_id == user.tenant_id)
        .order_by(AuditReport.created_at.desc())
        .all()
    )
    return {
        "success": True,
        "data": [
            {
                "id": str(r.id),
                "url": r.url,
                "entered_url": r.entered_url,
                "status": r.status.value,
                "overall_score": r.overall_score,
                "pages_scanned": r.pages_scanned,
                "scan_quality": r.scan_quality.value if r.scan_quality else None,
                "scan_quality_reason": r.scan_quality_reason,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# GET /audit/{audit_id}/entries — staged entries (full content, post-auth)
# ---------------------------------------------------------------------------


@router.get("/audit/{audit_id}/entries")
def list_entries(
    audit_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    _owned_report(db, audit_id, user.tenant_id)
    entries = (
        db.query(AuditEntry)
        .filter(AuditEntry.audit_report_id == audit_id)
        .order_by(AuditEntry.importance_rank.asc())
        .all()
    )
    return {
        "success": True,
        "data": [
            {
                "id": str(e.id),
                "url": e.url,
                "title": e.title,
                "category": e.category,
                "kind": e.kind.value,
                "status": e.status.value,
                "importance_rank": e.importance_rank,
                "edited": e.edited,
            }
            for e in entries
        ],
    }


# ---------------------------------------------------------------------------
# GET /audit/{audit_id}/gaps — gap list for the tenant's audit
# ---------------------------------------------------------------------------


@router.get("/audit/{audit_id}/gaps")
def list_gaps(
    audit_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    report = _owned_report(db, audit_id, user.tenant_id)
    return {"success": True, "data": report.gaps or []}


# ---------------------------------------------------------------------------
# GET /audit/entries/{entry_id} — full entry detail (for edit modal)
# ---------------------------------------------------------------------------


@router.get("/audit/entries/{entry_id}")
def get_audit_entry(
    entry_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    entry = (
        db.query(AuditEntry)
        .filter(AuditEntry.id == entry_id, AuditEntry.tenant_id == user.tenant_id)
        .one_or_none()
    )
    if entry is None:
        return _err("ENTRY_NOT_FOUND", "Entry not found", 404)
    return {
        "success": True,
        "data": EntryDetailResponse(
            id=str(entry.id),
            url=entry.url,
            title=entry.title,
            category=entry.category,
            kind=entry.kind.value if hasattr(entry.kind, "value") else str(entry.kind),
            content=entry.content or "",
            status=entry.status.value if hasattr(entry.status, "value") else str(entry.status),
            importance_rank=entry.importance_rank,
            edited=bool(entry.edited),
        ).model_dump(),
    }


# ---------------------------------------------------------------------------
# PATCH /audit/entries/{entry_id} — edit a staged entry
# ---------------------------------------------------------------------------


@router.patch("/audit/entries/{entry_id}")
def edit_entry(
    entry_id: UUID,
    payload: EntryEditRequest,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    entry = (
        db.query(AuditEntry)
        .filter(AuditEntry.id == entry_id, AuditEntry.tenant_id == user.tenant_id)
        .one_or_none()
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    if payload.title is not None:
        entry.title = payload.title
    if payload.kind is not None:
        try:
            entry.kind = AuditEntryKind(payload.kind)
        except ValueError:
            return _err(
                "INVALID_KIND",
                f"'{payload.kind}' is not a valid entry kind.",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
    if payload.content is not None:
        entry.content = payload.content
    if payload.category is not None:
        entry.category = payload.category
    if payload.status is not None:
        try:
            new_status = AuditEntryStatus(payload.status)
        except ValueError:
            return _err(
                "INVALID_STATUS",
                f"'{payload.status}' is not a valid entry status.",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        # Guard: activating a non-ACTIVE entry must not exceed the plan limit.
        # Audit findings share the tenant-global max_knowledge_sources cap with
        # uploaded files/URLs, so usage counts BOTH (not just this report's entries).
        if new_status == AuditEntryStatus.ACTIVE and entry.status != AuditEntryStatus.ACTIVE:
            svc = AuditService(db)
            max_sources = svc._max_sources(user.tenant_id)
            if max_sources is not None:
                usage = svc._source_usage(user.tenant_id, entry.audit_report_id)
                if usage >= max_sources:
                    return _err(
                        "ACTIVE_LIMIT_REACHED",
                        (
                            f"Your plan allows {max_sources} knowledge source(s) total "
                            "(uploaded files plus audit findings). "
                            "Remove a source or lock another finding first."
                        ),
                        status.HTTP_403_FORBIDDEN,
                    )
        entry.status = new_status

    entry.edited = True
    db.commit()
    return {"success": True, "data": {"id": str(entry.id), "edited": True}}


# ---------------------------------------------------------------------------
# POST /audit/{audit_id}/import — import selected active entries
# ---------------------------------------------------------------------------


@router.post("/audit/{audit_id}/import")
async def import_audit(
    audit_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    _owned_report(db, audit_id, user.tenant_id)
    svc = AuditService(db)
    svc.preselect_entries(audit_id)
    try:
        imported = await svc.import_entries(audit_id, uploaded_by=user.id)
    except RuntimeError:
        return _err(
            "ENQUEUE_FAILED",
            "Import could not be queued. Please try again later.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return {"success": True, "data": {"imported_count": len(imported)}}


# ---------------------------------------------------------------------------
# POST /audit/{audit_id}/gaps/{gap_index}/outline — forward stub (Stage 6)
# ---------------------------------------------------------------------------


@router.post(
    "/audit/{audit_id}/gaps/{gap_index}/outline",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
def generate_outline(
    audit_id: UUID,
    gap_index: int,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    _owned_report(db, audit_id, user.tenant_id)
    raise HTTPException(
        status_code=501,
        detail="Available with Marketing (Stage 6)",
    )
