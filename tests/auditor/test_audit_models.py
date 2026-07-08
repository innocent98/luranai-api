"""Tests for AuditReport + AuditEntry staging models and ProcessingJob nullable tenant."""


from app.db.models.audit import (
    AuditEntry,
    AuditEntryKind,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
    AuditStatus,
)


def test_audit_report_and_entry_persist(db):
    report = AuditReport(
        url="https://example.com",
        domain="example.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.QUEUED,
        claim_token="tok_abc",
    )
    db.add(report)
    db.flush()
    entry = AuditEntry(
        audit_report_id=report.id,
        url="https://example.com/faq",
        title="FAQ",
        category="customer onboarding faq",
        kind=AuditEntryKind.FAQ,
        content="Q: ... A: ...",
        content_hash="a" * 64,
        importance_rank=1,
        status=AuditEntryStatus.PROPOSED,
        meta={"currency": "NGN"},
    )
    db.add(entry)
    db.flush()
    db.refresh(report)
    assert report.scope.value == "public"
    assert report.entries[0].kind.value == "faq"
    assert report.entries[0].status.value == "proposed"


from app.db.models.job import JobStatus, JobType, ProcessingJob  # noqa: E402


def test_processing_job_allows_null_tenant(db):
    job = ProcessingJob(
        tenant_id=None,
        job_type=JobType.DOCUMENT_INGESTION,
        status=JobStatus.QUEUED,
        input_data={"kind": "website_audit"},
    )
    db.add(job)
    db.flush()
    db.refresh(job)
    assert job.id is not None
    assert job.tenant_id is None


def test_audit_report_scan_quality_fields(db):
    from app.db.models.audit import AuditReport, AuditScope, AuditStatus, ScanQuality

    r = AuditReport(
        tenant_id=None,
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        scan_quality=ScanQuality.THIN,
        scan_quality_reason="Only the homepage could be read.",
    )
    db.add(r)
    db.flush()
    db.refresh(r)
    assert r.scan_quality == ScanQuality.THIN
    assert r.scan_quality_reason == "Only the homepage could be read."


def test_audit_report_scan_quality_defaults_none(db):
    from app.db.models.audit import AuditReport, AuditScope, AuditStatus

    r = AuditReport(tenant_id=None, url="https://ex.com", domain="ex.com",
                    scope=AuditScope.PUBLIC, status=AuditStatus.QUEUED)
    db.add(r)
    db.flush()
    db.refresh(r)
    assert r.scan_quality is None
    assert r.scan_quality_reason is None
