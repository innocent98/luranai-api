"""Public (no-auth) Website Auditor endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auditor.service import AuditService
from app.db.models.audit import AuditReport, AuditScope
from app.db.models.lead import Lead
from app.schemas.auditor import LeadCaptureRequest, PublicAuditRequest, PublicReportResponse
from app.services.email_service import email_service
from app.utils.http_rate_limiter import create_rate_limit_dependency
from app.utils.rate_limiter import RedisRateLimiter

router = APIRouter()

public_audit_limiter = RedisRateLimiter(
    key_prefix="public_audit",
    max_calls=3,
    window_seconds=86400,
)

public_audit_rate_limit = create_rate_limit_dependency(public_audit_limiter, "ip")


def _err(code: str, message: str, http: int) -> JSONResponse:
    return JSONResponse(
        status_code=http,
        content={"success": False, "error": {"code": code, "message": message, "details": {}}},
    )


@router.post(
    "/audit",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(public_audit_rate_limit)],
)
async def start_public_audit(
    payload: PublicAuditRequest,
    db: Session = Depends(get_db),
):
    try:
        report = AuditService(db).start_audit(
            url=str(payload.url), scope=AuditScope.PUBLIC, tenant_id=None
        )
    except ValueError as exc:
        return _err("INVALID_URL", str(exc), status.HTTP_400_BAD_REQUEST)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "success": True,
            "data": {"audit_report_id": str(report.id), "claim_token": report.claim_token},
        },
    )


@router.get("/audit/{audit_id}")
def get_public_report(audit_id: UUID, db: Session = Depends(get_db)):
    report = (
        db.query(AuditReport)
        .filter(AuditReport.id == audit_id, AuditReport.scope == AuditScope.PUBLIC)
        .one_or_none()
    )
    if not report:
        return _err("AUDIT_NOT_FOUND", "Audit not found", status.HTTP_404_NOT_FOUND)
    blurred = [
        {
            "title": g.get("title"),
            "kind": g.get("kind"),
            "demand_count": g.get("demand_count", 0),
        }
        for g in (report.gaps or [])
    ]
    data = PublicReportResponse(
        id=report.id,
        status=report.status.value,
        pages_scanned=report.pages_scanned,
        knowledge_score=report.knowledge_score,
        support_score=report.support_score,
        conversion_score=report.conversion_score,
        overall_score=report.overall_score,
        projected_score=report.projected_score,
        gaps=blurred,
    )
    return {"success": True, "data": data.model_dump(mode="json")}


@router.post("/audit/{audit_id}/email")
def capture_lead(
    audit_id: UUID,
    payload: LeadCaptureRequest,
    db: Session = Depends(get_db),
):
    report = (
        db.query(AuditReport)
        .filter(AuditReport.id == audit_id, AuditReport.scope == AuditScope.PUBLIC)
        .one_or_none()
    )
    if not report:
        return _err("AUDIT_NOT_FOUND", "Audit not found", status.HTTP_404_NOT_FOUND)
    email = str(payload.email)
    lead = Lead(
        tenant_id=None,
        email=email,
        source="public_auditor",
        audit_report_id=report.id,
    )
    db.add(lead)
    db.flush()
    report.email = email
    report.lead_id = lead.id
    db.commit()
    email_service.send_templated_email(
        to_email=email,
        subject="Your AI Website Audit report",
        template_name="platform_broadcast.html",
        context={
            "recipient_name": email,
            "subject": "Your AI Website Audit report",
            "heading": "Your audit is ready",
            "body_html": f"Overall score: {report.overall_score}",
            "banner_label": "Website Audit",
            "accent_color": "#1d4ed8",
            "current_year": 2026,
        },
    )
    return {"success": True, "data": {"lead_id": str(lead.id)}}
