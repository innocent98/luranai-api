import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.auditor.service import AuditService
from app.db.models.audit import (
    AuditEntry, AuditEntryKind, AuditEntryStatus, AuditReport, AuditScope, AuditStatus,
)


def _public_report(db, *, age_days: int):
    report = AuditReport(tenant_id=None, url="https://ex.com", domain="ex.com",
                        scope=AuditScope.PUBLIC, status=AuditStatus.COMPLETED, claim_token="tok123")
    db.add(report)
    db.flush()
    report.created_at = datetime.now(UTC) - timedelta(days=age_days)
    db.add(AuditEntry(audit_report_id=report.id, tenant_id=None, url="https://ex.com/a", title="A",
                      category="c", kind=AuditEntryKind.FAQ, content="x", content_hash="h" * 64,
                      importance_rank=1, status=AuditEntryStatus.PROPOSED))
    db.flush()
    return report


def test_claim_fresh_reparents_report_and_entries(db):
    report = _public_report(db, age_days=1)
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit"):
        claimed = AuditService(db).claim_audit("tok123", tid)
    db.flush()
    assert claimed.tenant_id == tid
    entries = db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).all()
    assert all(e.tenant_id == tid for e in entries)


def test_claim_stale_triggers_fresh_crawl(db):
    report = _public_report(db, age_days=10)  # > 7 day staleness
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit") as mock_enqueue:
        AuditService(db).claim_audit("tok123", tid)
    mock_enqueue.assert_called_once()  # fresh crawl re-enqueued


def test_claim_recent_triggers_incremental_resume(db):
    report = _public_report(db, age_days=1)  # within staleness window
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit") as mock_enqueue:
        AuditService(db).claim_audit("tok123", tid)
    mock_enqueue.assert_called_once()  # incremental resume also enqueues


def test_claim_unknown_token_raises(db):
    tid = uuid.uuid4()
    import pytest
    with pytest.raises(ValueError, match="Invalid or already-claimed"):
        AuditService(db).claim_audit("no_such_token", tid)


def test_claim_already_claimed_token_raises(db):
    """A report whose tenant_id is already set should not be re-claimable."""
    tid = uuid.uuid4()
    report = AuditReport(tenant_id=tid, url="https://ex.com", domain="ex.com",
                         scope=AuditScope.PUBLIC, status=AuditStatus.COMPLETED,
                         claim_token="used_tok")
    db.add(report)
    db.flush()
    import pytest
    with pytest.raises(ValueError, match="Invalid or already-claimed"):
        AuditService(db).claim_audit("used_tok", uuid.uuid4())


def test_purge_removes_old_unclaimed(db):
    from app.auditor.service import purge_unclaimed_audits_sync
    old = _public_report(db, age_days=40)
    db.flush()
    removed = purge_unclaimed_audits_sync(db)
    assert removed >= 1
    assert db.query(AuditReport).filter(AuditReport.id == old.id).count() == 0


def test_purge_keeps_recent_unclaimed(db):
    """Reports within the purge window should not be deleted."""
    from app.auditor.service import purge_unclaimed_audits_sync
    recent = _public_report(db, age_days=5)
    db.flush()
    purge_unclaimed_audits_sync(db)
    assert db.query(AuditReport).filter(AuditReport.id == recent.id).count() == 1


def test_purge_keeps_claimed_reports(db):
    """Claimed reports (tenant_id set) must not be purged regardless of age."""
    from app.auditor.service import purge_unclaimed_audits_sync
    tid = uuid.uuid4()
    report = AuditReport(tenant_id=tid, url="https://ex.com", domain="ex.com",
                         scope=AuditScope.DASHBOARD, status=AuditStatus.COMPLETED,
                         claim_token=None)
    db.add(report)
    db.flush()
    # Backdate to be very old
    report.created_at = datetime.now(UTC) - timedelta(days=200)
    db.flush()
    purge_unclaimed_audits_sync(db)
    assert db.query(AuditReport).filter(AuditReport.id == report.id).count() == 1
