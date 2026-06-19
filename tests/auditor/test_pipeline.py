"""Tests for app/auditor/pipeline.py — end-to-end pipeline + failure path."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auditor import pipeline
from app.db.models.audit import AuditReport, AuditScope, AuditStatus, AuditEntryStatus
from app.db.models.job import JobStatus, JobType, ProcessingJob


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_job_and_report(db, *, tenant_id=None, scope=AuditScope.DASHBOARD):
    """Create a ProcessingJob + AuditReport pair flushed to *db*."""
    job = ProcessingJob(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        job_type=JobType.DOCUMENT_INGESTION,
        status=JobStatus.QUEUED,
        input_data={},
    )
    db.add(job)
    report = AuditReport(
        tenant_id=tenant_id,
        url="https://ex.com",
        domain="ex.com",
        scope=scope,
        status=AuditStatus.QUEUED,
        processing_job_id=job.id,
    )
    db.add(report)
    db.flush()
    return job, report


def _fake_fetch_with_sitemap(url):
    async def _inner(u):
        if u.endswith("sitemap.xml"):
            return "<urlset><url><loc>https://ex.com/about</loc></url></urlset>"
        return None
    return _inner(url)


async def _fake_fetch(url):
    if url.endswith("sitemap.xml"):
        return "<urlset><url><loc>https://ex.com/about</loc></url></urlset>"
    return None


async def _fake_renderer(url):
    return (
        200,
        "<html><head><title>About</title></head>"
        "<body><article><p>Lagos fintech, wallets.</p></article></body></html>",
    )


_PROF = {
    "summary": "fintech",
    "industry": "fintech",
    "offerings": ["wallet"],
    "audience": [],
    "positioning": "",
    "key_entities": [],
    "tone": {},
    "confidence": 0.8,
}
_CLS = {"category": "about", "kind": "product", "importance_rank": 1, "keep": True}
_EXPECTED = {
    "expected": [
        {
            "item": "Refund policy",
            "dimensions": ["support"],
            "weight": 1.0,
            "kind": "policy",
            "why_it_matters": "trust",
            "recommended_content": "add it",
            "demand_count": 2,
        }
    ]
}


# ---------------------------------------------------------------------------
# Test 1: happy-path tenant audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_audit_end_to_end_with_mocked_llm(db):
    tenant_id = uuid.uuid4()
    job, report = _make_job_and_report(db, tenant_id=tenant_id)

    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value=_EXPECTED)),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch
        )

    db.refresh(out)
    assert out.status == AuditStatus.COMPLETED
    assert out.pages_scanned >= 1
    assert out.business_profile_id is not None
    assert out.overall_score is not None
    assert len(out.gaps) == 1 and out.gaps[0]["title"] == "Refund policy"
    entries = [e for e in out.entries if e.status == AuditEntryStatus.PROPOSED]
    assert len(entries) >= 1
    db.refresh(job)
    assert job.status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# Test 2: failure path — BOTH report AND job must end FAILED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_audit_failure_marks_job_and_report_failed(db):
    """A phase failure must mark the report FAILED and job FAILED; must not raise."""
    tenant_id = uuid.uuid4()
    job, report = _make_job_and_report(db, tenant_id=tenant_id)

    async def boom_renderer(url):
        raise RuntimeError("network failure")

    async def fake_fetch(url):
        return None

    # Make profile synthesis raise to simulate a mid-pipeline failure.
    out = None
    with patch("app.auditor.profile.complete_json", AsyncMock(side_effect=ValueError("LLM down"))):
        try:
            out = await pipeline.run_audit(
                db, report.id, page_renderer=boom_renderer, fetch_text=fake_fetch
            )
        except Exception:
            pytest.fail("run_audit must not raise — it should handle failures internally")

    # run_audit re-queries after rollback; use the returned instance which is fresh.
    assert out is not None
    assert out.status == AuditStatus.FAILED
    # Re-query the job too — the test's local `job` reference may be stale after rollback.
    from app.db.models.job import ProcessingJob as _PJ
    fresh_job = db.query(_PJ).filter(_PJ.id == job.id).one()
    assert fresh_job.status == JobStatus.FAILED


# ---------------------------------------------------------------------------
# Test 3: reuse — second audit over SAME pages must NOT re-call profile LLM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_audit_reuse_skips_profile_llm_on_unchanged_pages(db):
    """Re-auditing unchanged pages for a tenant must not invoke the profile LLM twice."""
    tenant_id = uuid.uuid4()

    profile_mock = AsyncMock(return_value=_PROF)
    cls_mock = AsyncMock(return_value=_CLS)
    gaps_mock = AsyncMock(return_value=_EXPECTED)

    with (
        patch("app.auditor.profile.complete_json", profile_mock),
        patch("app.auditor.classify.complete_json", cls_mock),
        patch("app.auditor.gaps.complete_json", gaps_mock),
    ):
        # --- First audit ---
        job1, report1 = _make_job_and_report(db, tenant_id=tenant_id)
        await pipeline.run_audit(
            db, report1.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch
        )
        calls_after_first = profile_mock.call_count
        assert calls_after_first >= 1, "First audit must call the profile LLM"

        # --- Second audit — same URL/pages ---
        job2, report2 = _make_job_and_report(db, tenant_id=tenant_id)
        await pipeline.run_audit(
            db, report2.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch
        )
        calls_after_second = profile_mock.call_count

    assert calls_after_second == calls_after_first, (
        "Second audit over unchanged pages must NOT re-invoke the profile LLM "
        f"(call count went from {calls_after_first} to {calls_after_second})"
    )


# ---------------------------------------------------------------------------
# Test 4: public path — tenant_id=None, no BusinessProfile row, job COMPLETED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_audit_public_path(db):
    """Public audit (tenant_id=None): in-memory profile, no BusinessProfile row, entries saved."""
    from app.db.models.business_profile import BusinessProfile

    job, report = _make_job_and_report(db, tenant_id=None, scope=AuditScope.PUBLIC)

    profile_mock = AsyncMock(return_value=_PROF)
    cls_mock = AsyncMock(return_value=_CLS)
    gaps_mock = AsyncMock(return_value=_EXPECTED)

    with (
        patch("app.auditor.profile.complete_json", profile_mock),
        patch("app.auditor.classify.complete_json", cls_mock),
        patch("app.auditor.gaps.complete_json", gaps_mock),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch
        )

    db.refresh(out)

    # (a) No persisted BusinessProfile row for this non-existent tenant
    bp_count = db.query(BusinessProfile).filter(BusinessProfile.tenant_id == None).count()  # noqa: E711
    assert bp_count == 0, "Public audit must not persist a BusinessProfile row"
    assert out.business_profile_id is None, "Public report must have no business_profile_id"

    # (b) AuditEntry rows have tenant_id=None
    from app.db.models.audit import AuditEntry
    entries = db.query(AuditEntry).filter(AuditEntry.audit_report_id == out.id).all()
    assert len(entries) >= 1
    assert all(e.tenant_id is None for e in entries)

    # (c) Scores produced and job COMPLETED
    assert out.overall_score is not None
    assert out.gaps is not None
    assert out.status == AuditStatus.COMPLETED
    db.refresh(job)
    assert job.status == JobStatus.COMPLETED
