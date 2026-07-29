"""Tests for reap_stalled_audits_sync — fail audits stranded by a killed worker."""

from datetime import UTC, datetime, timedelta

from app.auditor.service import reap_stalled_audits_sync
from app.db.models.audit import AuditReport, AuditScope, AuditStatus, ScanQuality


def _report(db, status, age_minutes):
    r = AuditReport(
        tenant_id=None,
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.PUBLIC,
        status=status,
    )
    r.created_at = datetime.now(UTC) - timedelta(minutes=age_minutes)
    db.add(r)
    db.flush()
    return r


def test_reaps_stale_crawling_report(db):
    r = _report(db, AuditStatus.CRAWLING, age_minutes=120)
    n = reap_stalled_audits_sync(db, older_than_minutes=45)
    db.refresh(r)
    assert n >= 1
    assert r.status == AuditStatus.FAILED
    assert r.scan_quality == ScanQuality.RENDER_FAILED
    assert r.scan_quality_reason  # honest, non-empty reason


def test_reaps_stale_analyzing_and_queued(db):
    a = _report(db, AuditStatus.ANALYZING, age_minutes=90)
    q = _report(db, AuditStatus.QUEUED, age_minutes=90)
    reap_stalled_audits_sync(db, older_than_minutes=45)
    db.refresh(a)
    db.refresh(q)
    assert a.status == AuditStatus.FAILED
    assert q.status == AuditStatus.FAILED


def test_leaves_fresh_crawling_report(db):
    r = _report(db, AuditStatus.CRAWLING, age_minutes=5)
    reap_stalled_audits_sync(db, older_than_minutes=45)
    db.refresh(r)
    assert r.status == AuditStatus.CRAWLING


def test_leaves_completed_report(db):
    r = _report(db, AuditStatus.COMPLETED, age_minutes=200)
    reap_stalled_audits_sync(db, older_than_minutes=45)
    db.refresh(r)
    assert r.status == AuditStatus.COMPLETED
