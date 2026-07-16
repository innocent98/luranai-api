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
async def test_start_audit_job_has_website_audit_type(db):
    """ProcessingJob created by start_audit must have job_type == WEBSITE_AUDIT."""
    from app.db.models.job import JobType
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        svc = AuditService(db)
        report = await svc.start_audit(url="https://example.com/", scope=AuditScope.PUBLIC, tenant_id=None)
    db.flush()
    job = db.query(ProcessingJob).filter(ProcessingJob.id == report.processing_job_id).one()
    assert job.job_type == JobType.WEBSITE_AUDIT


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


# ---------------------------------------------------------------------------
# Submit-time target validation.
#
# The crawl guard rejects an unresolvable/unsafe host anyway, but only after a
# report + job exist and a worker has picked the job up — and it surfaces as
# RENDER_FAILED ("something went wrong on our end"). Catching it here turns a
# typo into an immediate, accurate 400 and leaves no orphan rows behind.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_audit_rejects_unresolvable_domain(db):
    from app.utils.network_security import HostResolutionError

    with patch(
        "app.auditor.service.resolve_and_validate_host",
        side_effect=HostResolutionError("Could not resolve hostname: nope.example"),
    ), patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock) as mock_enqueue:
        svc = AuditService(db)
        with pytest.raises(ValueError) as exc:
            await svc.start_audit(url="https://nope.example/", scope=AuditScope.PUBLIC, tenant_id=None)

    # The message must name the domain and blame the URL, not our renderer.
    assert "nope.example" in str(exc.value)
    assert "our end" not in str(exc.value).lower()
    mock_enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_audit_creates_no_rows_when_target_invalid(db):
    from app.utils.network_security import HostResolutionError

    before_reports = db.query(AuditReport).count()
    before_jobs = db.query(ProcessingJob).count()
    with patch(
        "app.auditor.service.resolve_and_validate_host",
        side_effect=HostResolutionError("Could not resolve hostname: nope.example"),
    ), patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        with pytest.raises(ValueError):
            await AuditService(db).start_audit(
                url="https://nope.example/", scope=AuditScope.PUBLIC, tenant_id=None
            )
    db.rollback()
    assert db.query(AuditReport).count() == before_reports
    assert db.query(ProcessingJob).count() == before_jobs


@pytest.mark.asyncio
async def test_start_audit_rejects_unsafe_host_without_leaking_internals(db):
    with patch(
        "app.auditor.service.resolve_and_validate_host",
        side_effect=ValueError("URL resolves to disallowed IP: 127.0.0.1"),
    ), patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock) as mock_enqueue:
        with pytest.raises(ValueError) as exc:
            await AuditService(db).start_audit(
                url="https://rebound.example/", scope=AuditScope.PUBLIC, tenant_id=None
            )
    # Never echo the resolved IP or our guard's internals back to the caller.
    assert "127.0.0.1" not in str(exc.value)
    mock_enqueue.assert_not_awaited()
