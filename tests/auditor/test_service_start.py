import uuid
from unittest.mock import patch

from app.auditor.service import AuditService
from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.job import ProcessingJob


def test_start_audit_public_creates_report_job_and_claim_token(db):
    with patch("app.auditor.service.enqueue_audit") as mock_enqueue:
        svc = AuditService(db)
        report = svc.start_audit(url="https://Example.com/", scope=AuditScope.PUBLIC, tenant_id=None)
    db.flush()
    assert report.scope == AuditScope.PUBLIC
    assert report.status == AuditStatus.QUEUED
    assert report.domain == "example.com"
    assert report.claim_token  # public reports are claimable
    assert report.processing_job_id is not None
    assert db.query(ProcessingJob).filter(ProcessingJob.id == report.processing_job_id).count() == 1
    mock_enqueue.assert_called_once_with(report.id)


def test_start_audit_dashboard_has_tenant_no_claim_token(db):
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit"):
        report = AuditService(db).start_audit(
            url="https://shop.example.com", scope=AuditScope.DASHBOARD, tenant_id=tid
        )
    db.flush()
    assert report.tenant_id == tid
    assert report.claim_token is None
