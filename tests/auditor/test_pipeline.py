"""Tests for app/auditor/pipeline.py — end-to-end pipeline + failure path."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import pipeline
from app.db.models.audit import AuditReport, AuditScope, AuditStatus, AuditEntryStatus
from app.db.models.job import JobStatus, JobType, ProcessingJob


@pytest.mark.asyncio
async def test_run_audit_end_to_end_with_mocked_llm(db):
    tenant_id = uuid.uuid4()
    job = ProcessingJob(
        id=uuid.uuid4(), tenant_id=tenant_id, job_type=JobType.DOCUMENT_INGESTION,
        status=JobStatus.QUEUED, input_data={},
    )
    db.add(job)
    report = AuditReport(
        tenant_id=tenant_id, url="https://ex.com", domain="ex.com",
        scope=AuditScope.DASHBOARD, status=AuditStatus.QUEUED, processing_job_id=job.id,
    )
    db.add(report)
    db.flush()

    async def fake_fetch(url):
        if url.endswith("sitemap.xml"):
            return "<urlset><url><loc>https://ex.com/about</loc></url></urlset>"
        return None

    async def fake_renderer(url):
        return 200, "<html><head><title>About</title></head><body><article><p>Lagos fintech, wallets.</p></article></body></html>"

    prof = {"summary": "fintech", "industry": "fintech", "offerings": ["wallet"],
            "audience": [], "positioning": "", "key_entities": [], "tone": {}, "confidence": 0.8}
    cls = {"category": "about", "kind": "product", "importance_rank": 1, "keep": True}
    expected = {"expected": [{"item": "Refund policy", "dimensions": ["support"], "weight": 1.0,
                              "kind": "policy", "why_it_matters": "trust",
                              "recommended_content": "add it", "demand_count": 2}]}

    with patch("app.auditor.profile.complete_json", AsyncMock(return_value=prof)), \
         patch("app.auditor.classify.complete_json", AsyncMock(return_value=cls)), \
         patch("app.auditor.gaps.complete_json", AsyncMock(return_value=expected)):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=fake_renderer, fetch_text=fake_fetch
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


@pytest.mark.asyncio
async def test_run_audit_failure_marks_job_and_report_failed(db):
    """A phase failure must mark the report FAILED and job FAILED; must not raise."""
    tenant_id = uuid.uuid4()
    job = ProcessingJob(
        id=uuid.uuid4(), tenant_id=tenant_id, job_type=JobType.DOCUMENT_INGESTION,
        status=JobStatus.QUEUED, input_data={},
    )
    db.add(job)
    report = AuditReport(
        tenant_id=tenant_id, url="https://ex.com", domain="ex.com",
        scope=AuditScope.DASHBOARD, status=AuditStatus.QUEUED, processing_job_id=job.id,
    )
    db.add(report)
    db.flush()

    async def fake_fetch(url):
        return None

    async def boom_renderer(url):
        raise RuntimeError("network failure")

    # discovery will find no URLs (sitemap=None, no crawl); crawl returns [];
    # we make profile synthesis raise to simulate a mid-pipeline failure
    async def bad_profile(*args, **kwargs):
        raise ValueError("LLM down")

    with patch("app.auditor.profile.complete_json", AsyncMock(side_effect=bad_profile)):
        try:
            await pipeline.run_audit(
                db, report.id, page_renderer=boom_renderer, fetch_text=fake_fetch
            )
        except Exception:
            pytest.fail("run_audit must not raise — it should handle failures internally")

    db.refresh(report)
    db.refresh(job)
    assert report.status == AuditStatus.FAILED
    assert job.status == JobStatus.FAILED
