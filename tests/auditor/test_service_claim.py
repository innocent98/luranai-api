import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor.service import AuditService
from app.db.models.audit import (
    AuditEntry, AuditEntryKind, AuditEntryStatus, AuditReport, AuditScope, AuditStatus,
)
from app.db.models.job import ProcessingJob


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


@pytest.mark.asyncio
async def test_claim_fresh_reparents_report_and_entries(db):
    report = _public_report(db, age_days=1)
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        claimed = await AuditService(db).claim_audit("tok123", tid)
    db.flush()
    assert claimed.tenant_id == tid
    entries = db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).all()
    assert all(e.tenant_id == tid for e in entries)


@pytest.mark.asyncio
async def test_claim_stale_triggers_fresh_crawl(db):
    _public_report(db, age_days=10)  # > 7 day staleness
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock) as mock_enqueue:
        await AuditService(db).claim_audit("tok123", tid)
    mock_enqueue.assert_awaited_once()  # fresh crawl re-enqueued
    # Assert resume=False for stale (fresh-crawl) branch
    job = db.query(ProcessingJob).filter(ProcessingJob.tenant_id == tid).one()
    assert job.input_data["resume"] is False


@pytest.mark.asyncio
async def test_claim_recent_triggers_incremental_resume(db):
    _public_report(db, age_days=1)  # within staleness window
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock) as mock_enqueue:
        await AuditService(db).claim_audit("tok123", tid)
    mock_enqueue.assert_awaited_once()  # incremental resume also enqueues
    # Assert resume=True for recent (incremental-resume) branch
    job = db.query(ProcessingJob).filter(ProcessingJob.tenant_id == tid).one()
    assert job.input_data["resume"] is True


@pytest.mark.asyncio
async def test_claim_unknown_token_raises(db):
    tid = uuid.uuid4()
    with pytest.raises(ValueError, match="Invalid or already-claimed"):
        await AuditService(db).claim_audit("no_such_token", tid)


@pytest.mark.asyncio
async def test_claim_already_claimed_token_raises(db):
    """A report whose tenant_id is already set should not be re-claimable."""
    tid = uuid.uuid4()
    report = AuditReport(tenant_id=tid, url="https://ex.com", domain="ex.com",
                         scope=AuditScope.PUBLIC, status=AuditStatus.COMPLETED,
                         claim_token="used_tok")
    db.add(report)
    db.flush()
    with pytest.raises(ValueError, match="Invalid or already-claimed"):
        await AuditService(db).claim_audit("used_tok", uuid.uuid4())


@pytest.mark.asyncio
async def test_claim_reparents_business_profile(db):
    """Claiming a report with a linked BusinessProfile re-parents the BP to the tenant.

    BusinessProfile.tenant_id is NOT NULL in schema, so we can't store NULL in
    SQLite. Instead we stub the session query to return a BP object whose
    tenant_id is None in Python — exactly what the service checks — and assert
    the re-parent branch sets it to the claiming tenant.
    """
    from unittest.mock import MagicMock

    from app.db.models.business_profile import BusinessProfile, BusinessProfileSource

    bp_id = uuid.uuid4()
    tid = uuid.uuid4()

    # Build a transient BP with tenant_id=None (bypasses DB NOT NULL constraint).
    # Use a real placeholder then override via __dict__ after SA instrumentation
    # has run its __init__.
    placeholder_tid = uuid.uuid4()
    fake_bp = BusinessProfile(tenant_id=placeholder_tid, source=BusinessProfileSource.AUDITOR)
    fake_bp.id = bp_id
    # Override tenant_id directly in the instance dict, bypassing SA descriptors
    # so the Python attribute reads as None without touching the DB column.
    fake_bp.__dict__["tenant_id"] = None

    # Create the public report pointing at the fake BP
    report = AuditReport(
        tenant_id=None,
        url="https://bp-test.com",
        domain="bp-test.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        claim_token="bp_tok",
        business_profile_id=bp_id,
    )
    db.add(report)
    db.flush()
    report.created_at = datetime.now(UTC) - timedelta(days=1)
    db.flush()

    # Patch the session's BP query to return our fake object
    real_query = db.query

    def patched_query(model, *args, **kwargs):
        q = real_query(model, *args, **kwargs)
        if model is BusinessProfile:
            mock_q = MagicMock()
            mock_q.filter.return_value.one_or_none.return_value = fake_bp
            return mock_q
        return q

    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock), \
         patch.object(db, "query", side_effect=patched_query):
        await AuditService(db).claim_audit("bp_tok", tid)

    assert fake_bp.tenant_id == tid


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
