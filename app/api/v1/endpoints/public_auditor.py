"""Public (no-auth) Website Auditor endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.auditor.service import AuditService
from app.core.config import settings
from app.db.models.audit import AuditReport, AuditScope
from app.db.models.lead import Lead
from app.schemas.auditor import LeadCaptureRequest, PublicAuditRequest, PublicReportResponse
from app.services.email_service import email_service
from app.services.lead_service import LeadService
from app.utils.http_rate_limiter import create_rate_limit_dependency
from app.utils.rate_limiter import RedisRateLimiter

router = APIRouter()

public_audit_limiter = RedisRateLimiter(
    key_prefix="public_audit",
    max_calls=3,
    window_seconds=86400,
)

public_audit_rate_limit = create_rate_limit_dependency(public_audit_limiter, "ip")

capture_lead_limiter = RedisRateLimiter(
    key_prefix="public_audit_email",
    max_calls=5,
    window_seconds=3600,
)

capture_lead_rate_limit = create_rate_limit_dependency(capture_lead_limiter, "ip")


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
        report = await AuditService(db).start_audit(
            url=str(payload.url), scope=AuditScope.PUBLIC, tenant_id=None
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
        scan_quality=report.scan_quality.value if report.scan_quality else None,
        scan_quality_reason=report.scan_quality_reason,
    )
    return {"success": True, "data": data.model_dump(mode="json")}


@router.post("/audit/{audit_id}/email", dependencies=[Depends(capture_lead_rate_limit)])
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
    # Idempotency: if a lead was already captured for this report, reject.
    if report.lead_id is not None:
        return _err(
            "LEAD_ALREADY_CAPTURED",
            "A lead has already been captured for this report.",
            status.HTTP_409_CONFLICT,
        )
    email = str(payload.email)
    lead = Lead(
        tenant_id=None,
        email=email,
        source="public_auditor",
        audit_report_id=report.id,
    )
    LeadService(db).apply_score(lead)
    db.add(lead)
    db.flush()
    report.email = email
    report.email_hash = Lead.compute_email_hash(email)
    report.lead_id = lead.id
    db.commit()
    gap_titles = [g.get("title") for g in (report.gaps or []) if g.get("title")]
    # Signup/unlock link: use the configured frontend (luranai.com in prod), the
    # real /register route (matches the public report CTA), and carry the report's
    # claim token so signing up claims this scan and resumes the full audit.
    base = settings.FRONTEND_URL.rstrip("/")
    signup_url = (
        f"{base}/register?claim_token={report.claim_token}"
        if report.claim_token
        else f"{base}/register"
    )
    email_service.send_templated_email(
        to_email=email,
        subject="Your AI Website Audit report",
        template_name="audit_report.html",
        context={
            "recipient_email": email,
            "subject": "Your AI Website Audit report",
            "website_url": report.url,
            "knowledge_score": report.knowledge_score,
            "support_score": report.support_score,
            "conversion_score": report.conversion_score,
            "overall_score": report.overall_score,
            "projected_score": report.projected_score,
            "gap_titles": gap_titles,
            "signup_url": signup_url,
            "current_year": 2026,
        },
    )
    return {"success": True, "data": {"lead_id": str(lead.id)}}
