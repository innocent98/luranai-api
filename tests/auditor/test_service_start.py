import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor.service import AuditService
from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.job import ProcessingJob


@pytest.mark.asyncio
async def test_start_audit_public_creates_report_job_and_claim_token(db):
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock) as mock_enqueue:
        svc = AuditService(db)
        report = await svc.start_audit(url="https://Example.com/", scope=AuditScope.PUBLIC, tenant_id=None)
    db.flush()
    assert report.scope == AuditScope.PUBLIC
    assert report.status == AuditStatus.QUEUED
    assert report.domain == "example.com"
    assert report.claim_token  # public reports are claimable
    assert report.processing_job_id is not None
    assert db.query(ProcessingJob).filter(ProcessingJob.id == report.processing_job_id).count() == 1
    mock_enqueue.assert_awaited_once_with(report.id)


@pytest.mark.asyncio
async def test_start_audit_dashboard_has_tenant_no_claim_token(db):
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        report = await AuditService(db).start_audit(
            url="https://shop.example.com", scope=AuditScope.DASHBOARD, tenant_id=tid
        )
    db.flush()
    assert report.tenant_id == tid
    assert report.claim_token is None


@pytest.mark.asyncio
async def test_start_audit_enqueue_failure_marks_report_failed_and_raises(db):
    """When enqueue_audit raises, the report is marked FAILED and RuntimeError propagates."""
    with patch(
        "app.auditor.service.enqueue_audit",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Redis down"),
    ):
        with pytest.raises(RuntimeError, match="Audit enqueue failed"):
            await AuditService(db).start_audit(
                url="https://example.com", scope=AuditScope.PUBLIC, tenant_id=None
            )

    # The report must exist in the DB and be marked FAILED (not stuck QUEUED)
    failed = db.query(AuditReport).filter(AuditReport.status == AuditStatus.FAILED).all()
    assert len(failed) == 1
    assert failed[0].domain == "example.com"
