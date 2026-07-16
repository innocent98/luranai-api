"""Tests for app/auditor/pipeline.py — end-to-end pipeline + failure path."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import pipeline
from app.db.models.audit import (
    AuditEntry,
    AuditEntryKind,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
    AuditStatus,
)
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
    from app.db.models.job import ProcessingJob as PJob

    fresh_job = db.query(PJob).filter(PJob.id == job.id).one()
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
    bp_count = (
        db.query(BusinessProfile).filter(BusinessProfile.tenant_id == None).count()
    )  # noqa: E711
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


# ---------------------------------------------------------------------------
# Test 5: fresh re-run must not duplicate AuditEntry rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_rerun_no_duplicate_entries(db):
    """Running the pipeline twice with resume=False must yield exactly one set of entries."""
    tenant_id = uuid.uuid4()

    profile_mock = AsyncMock(return_value=_PROF)
    cls_mock = AsyncMock(return_value=_CLS)
    gaps_mock = AsyncMock(return_value=_EXPECTED)

    with (
        patch("app.auditor.profile.complete_json", profile_mock),
        patch("app.auditor.classify.complete_json", cls_mock),
        patch("app.auditor.gaps.complete_json", gaps_mock),
    ):
        job, report = _make_job_and_report(db, tenant_id=tenant_id)

        # First run
        await pipeline.run_audit(
            db, report.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch
        )
        count_after_first = (
            db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).count()
        )
        assert count_after_first >= 1, "First run must produce at least one entry"

        # Second run — same report, resume=False (fresh)
        await pipeline.run_audit(
            db, report.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch, resume=False
        )
        count_after_second = (
            db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).count()
        )

    assert (
        count_after_second == count_after_first
    ), f"Fresh re-run doubled entries: {count_after_first} → {count_after_second}"


# ---------------------------------------------------------------------------
# Test 6: incremental resume — only new URLs crawled, existing entries kept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_resume_appends_only_new_urls(db):
    """resume=True must crawl only new URLs and append entries without touching existing ones."""
    tenant_id = uuid.uuid4()
    job, report = _make_job_and_report(db, tenant_id=tenant_id)

    # Pre-seed N=2 existing AuditEntry rows (simulating a prior completed audit)
    existing_urls = ["https://ex.com/old-a", "https://ex.com/old-b"]
    for url in existing_urls:
        db.add(
            AuditEntry(
                audit_report_id=report.id,
                tenant_id=tenant_id,
                url=url,
                title="Old page",
                category="about",
                kind=AuditEntryKind.PRODUCT,
                content="old content",
                content_hash=f"hash-{url}",
                importance_rank=5,
                status=AuditEntryStatus.PROPOSED,
                meta={},
            )
        )
    db.flush()
    n_existing = len(existing_urls)

    # discover returns existing + 1 new URL
    new_url = "https://ex.com/new-page"
    all_discovered = existing_urls + [new_url]

    # Track which URLs the renderer is actually called for
    crawled_urls: list[str] = []

    async def tracking_renderer(url: str):
        crawled_urls.append(url)
        return (
            200,
            f"<html><head><title>Page {url}</title></head>"
            f"<body><p>content for {url}</p></body></html>",
        )

    profile_mock = AsyncMock(return_value=_PROF)
    cls_mock = AsyncMock(return_value=_CLS)
    gaps_mock = AsyncMock(return_value=_EXPECTED)

    with (
        patch("app.auditor.profile.complete_json", profile_mock),
        patch("app.auditor.classify.complete_json", cls_mock),
        patch("app.auditor.gaps.complete_json", gaps_mock),
        patch(
            "app.auditor.discovery.seed_urls",
            AsyncMock(return_value=all_discovered),
        ),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=tracking_renderer, fetch_text=_fake_fetch, resume=True
        )

    db.refresh(out)

    # (a) Only the new URL was passed to the renderer
    assert crawled_urls == [
        new_url
    ], f"Expected only [{new_url!r}] to be crawled; got {crawled_urls}"

    # (b) + (c) existing entries survive and total == N + M (M=1 new)
    all_entries = db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).all()
    assert (
        len(all_entries) == n_existing + 1
    ), f"Expected {n_existing + 1} entries total; got {len(all_entries)}"
    persisted_urls = {e.url for e in all_entries}
    for url in existing_urls:
        assert url in persisted_urls, f"Existing entry {url!r} was deleted"
    assert new_url in persisted_urls, "New entry was not added"

    # (d) scores and gaps were recomputed (report completed successfully)
    assert out.status == AuditStatus.COMPLETED
    assert out.overall_score is not None
    assert out.gaps is not None

    # (e) profile LLM reuse — synthesize_or_reuse must not re-call the LLM
    # (the existing BusinessProfile from the prior run has the same content signature
    # as the unchanged pages).  profile_mock.call_count covers synthesize calls only;
    # for this test we assert it was called at most once (reuse path keeps it at 0 or 1
    # depending on whether a profile row exists; the key invariant is no duplicate call).
    assert (
        profile_mock.call_count <= 1
    ), f"Profile LLM called {profile_mock.call_count} times; expected ≤1 (reuse)"


# ---------------------------------------------------------------------------
# Test 7: scoring/gaps alignment — category-label match counts in both
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_and_gaps_agree_on_category_label_match(db):
    """An expected item found by category label (not kind) must be absent from gaps
    AND counted in the dimension score — score and gaps must tell the same story."""
    tenant_id = uuid.uuid4()
    job, report = _make_job_and_report(db, tenant_id=tenant_id)

    # The expected map has one item whose kind ("faq") is NOT present on the site,
    # but whose item label ("faqs") matches the category of a crawled page.
    # gaps.compute_gaps counts it as found (label match); score must too.
    expected_category_match = {
        "expected": [
            {
                "item": "FAQs",  # label — lowercase "faqs" will match category below
                "dimensions": ["support"],
                "weight": 1.0,
                "kind": "faq",  # kind NOT in found_kinds (crawler returns "product")
                "why_it_matters": "answers questions",
                "recommended_content": "add faq page",
                "demand_count": 0,
            }
        ]
    }

    # Classify returns kind=product but category="faqs" — matches item label "faqs"
    cls_category_match = {
        "category": "faqs",  # lowercase matches item label "FAQs"
        "kind": "product",  # different from expected kind "faq"
        "importance_rank": 1,
        "keep": True,
    }

    profile_mock = AsyncMock(return_value=_PROF)
    cls_mock = AsyncMock(return_value=cls_category_match)
    gaps_mock = AsyncMock(return_value=expected_category_match)

    with (
        patch("app.auditor.profile.complete_json", profile_mock),
        patch("app.auditor.classify.complete_json", cls_mock),
        patch("app.auditor.gaps.complete_json", gaps_mock),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch
        )

    db.refresh(out)
    assert out.status == AuditStatus.COMPLETED

    # Gaps: item found by category label → NOT in gap list
    assert out.gaps == [], f"Item found by category match must not appear in gaps; got {out.gaps}"

    # Score: item found by category label → support dimension score must be > 0
    assert (
        out.support_score is not None and out.support_score > 0
    ), f"Item found by category match must contribute to support score; got {out.support_score}"


# ---------------------------------------------------------------------------
# Test 8: scan outcome — unreachable root fails the audit with a reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_audit_unreachable_root_fails_with_reason(db):
    from app.db.models.audit import ScanQuality

    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())

    async def refusing_renderer(url):
        raise ConnectionError("connection refused")

    async def no_sitemap(url):
        return None

    out = await pipeline.run_audit(
        db, report.id, page_renderer=refusing_renderer, fetch_text=no_sitemap
    )
    db.refresh(out)
    assert out.status == AuditStatus.FAILED
    assert out.scan_quality == ScanQuality.UNREACHABLE
    assert out.scan_quality_reason  # non-empty


# ---------------------------------------------------------------------------
# Test 9: scan outcome — browser launch failure => render_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_audit_launch_failure_is_render_failed(db):
    from app.db.models.audit import ScanQuality

    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())

    async def no_browser(url):
        raise Exception("BrowserType.launch: Executable doesn't exist at /x")

    async def no_sitemap(url):
        return None

    out = await pipeline.run_audit(db, report.id, page_renderer=no_browser, fetch_text=no_sitemap)
    db.refresh(out)
    assert out.status == AuditStatus.FAILED
    assert out.scan_quality == ScanQuality.RENDER_FAILED


# ---------------------------------------------------------------------------
# Test 10: scan outcome — thin content completes but is flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_audit_thin_content_completes_flagged(db):
    from app.db.models.audit import ScanQuality

    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())

    async def tiny_renderer(url):
        return 200, "<html><body><p>hi</p></body></html>"

    async def no_sitemap(url):
        return None

    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value=_EXPECTED)),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=tiny_renderer, fetch_text=no_sitemap
        )
    db.refresh(out)
    assert out.status == AuditStatus.COMPLETED
    assert out.scan_quality == ScanQuality.THIN


# ---------------------------------------------------------------------------
# Test 11: scan outcome — rich multi-page SPA => ok, multiple pages scanned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_audit_multipage_ok(db):
    from app.db.models.audit import ScanQuality

    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())

    long_text = "Luran builds AI customer agents. " * 60  # > 800 chars/page
    html_by_url = {
        "https://ex.com/": f"<html><body><article><p>{long_text}</p>"
        f'<a href="/features">F</a></article></body></html>',
        "https://ex.com/features": f"<html><body><article><p>{long_text} features</p>"
        f"</article></body></html>",
    }

    async def spa_renderer(url):
        return 200, html_by_url.get(url, "<html><body><p>x</p></body></html>")

    async def no_sitemap(url):
        return None

    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value=_EXPECTED)),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=spa_renderer, fetch_text=no_sitemap
        )
    db.refresh(out)
    assert out.status == AuditStatus.COMPLETED
    assert out.scan_quality == ScanQuality.OK
    assert out.pages_scanned >= 2  # root + /features discovered from rendered link


# ---------------------------------------------------------------------------
# Fix A: empty expected_map + real content => FAILED, not a fake 0-score report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_expected_map_with_content_fails_not_zero(db):
    from app.db.models.audit import ScanQuality

    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())
    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        # expected_map empty on every retry -> analyze failure
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value={"expected": []})),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch
        )
    db.refresh(out)
    # Must NOT publish a confident 0 — it's a failed analysis.
    assert out.status == AuditStatus.FAILED
    assert out.scan_quality == ScanQuality.RENDER_FAILED
    assert out.overall_score is None  # scoring never ran


# ---------------------------------------------------------------------------
# Fix B: a fresh re-run preserves the user's active-source selections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_rerun_preserves_active_selections(db):
    tid = uuid.uuid4()
    job, report = _make_job_and_report(db, tenant_id=tid)
    # Pre-seed an ACTIVE entry at the root URL the fresh crawl will re-create.
    db.add(
        AuditEntry(
            audit_report_id=report.id,
            tenant_id=tid,
            url="https://ex.com/",
            title="Home",
            category="about",
            kind=AuditEntryKind.PRODUCT,
            content="c",
            content_hash="h" * 64,
            importance_rank=1,
            status=AuditEntryStatus.ACTIVE,
            meta={},
        )
    )
    db.flush()
    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value=_EXPECTED)),
        patch("app.auditor.pipeline._active_source_cap", return_value=None),  # unlimited
    ):
        await pipeline.run_audit(
            db, report.id, page_renderer=_fake_renderer, fetch_text=_fake_fetch, resume=False
        )
    entries = db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).all()
    active_urls = {e.url for e in entries if e.status == AuditEntryStatus.ACTIVE}
    assert "https://ex.com/" in active_urls, (
        f"active selection not preserved across fresh re-run: "
        f"{[(e.url, e.status.value) for e in entries]}"
    )


@pytest.mark.asyncio
async def test_fresh_rerun_caps_restored_actives_at_plan_limit(db):
    """Restoring active selections on a fresh re-crawl must NOT exceed the plan
    cap — a downgraded tenant can't be pushed over max_knowledge_sources."""
    tid = uuid.uuid4()
    job, report = _make_job_and_report(db, tenant_id=tid)
    for u in ("https://ex.com/a", "https://ex.com/b"):
        db.add(
            AuditEntry(
                audit_report_id=report.id,
                tenant_id=tid,
                url=u,
                title=u,
                category="about",
                kind=AuditEntryKind.PRODUCT,
                content="c",
                content_hash="hash-" + u,
                importance_rank=1,
                status=AuditEntryStatus.ACTIVE,
                meta={},
            )
        )
    db.flush()

    async def fetch_ab(url):
        if url.endswith("sitemap.xml"):
            return (
                "<urlset><url><loc>https://ex.com/a</loc></url>"
                "<url><loc>https://ex.com/b</loc></url></urlset>"
            )
        return None

    async def distinct_renderer(url):
        return (
            200,
            f"<html><head><title>{url}</title></head><body><article>"
            f"<p>Unique readable content for {url} with several words to extract.</p>"
            f"</article></body></html>",
        )

    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value=_EXPECTED)),
        patch("app.auditor.pipeline._active_source_cap", return_value=1),  # plan cap = 1
    ):
        await pipeline.run_audit(
            db, report.id, page_renderer=distinct_renderer, fetch_text=fetch_ab, resume=False
        )

    active = (
        db.query(AuditEntry)
        .filter(
            AuditEntry.audit_report_id == report.id,
            AuditEntry.status == AuditEntryStatus.ACTIVE,
        )
        .count()
    )
    assert active == 1, f"restored actives must be capped at the plan limit; got {active}"


@pytest.mark.asyncio
async def test_fresh_rerun_restore_counts_existing_knowledge_sources(db):
    """Restore must respect the tenant-GLOBAL budget: an uploaded file already at
    the cap leaves zero slots for restored audit actives (no silent over-select)."""
    from app.db.models.knowledge import KnowledgeSource, SourceStatus, SourceType

    tid = uuid.uuid4()
    job, report = _make_job_and_report(db, tenant_id=tid)
    # An uploaded file already consuming the single cap slot.
    db.add(
        KnowledgeSource(
            tenant_id=tid,
            name="f",
            source_type=SourceType.OTHER,
            source_category="file",
            content_hash="file-0",
            version=1,
            status=SourceStatus.ACTIVE,
        )
    )
    for u in ("https://ex.com/a", "https://ex.com/b"):
        db.add(
            AuditEntry(
                audit_report_id=report.id,
                tenant_id=tid,
                url=u,
                title=u,
                category="about",
                kind=AuditEntryKind.PRODUCT,
                content="c",
                content_hash="hash-" + u,
                importance_rank=1,
                status=AuditEntryStatus.ACTIVE,
                meta={},
            )
        )
    db.flush()

    async def fetch_ab(url):
        if url.endswith("sitemap.xml"):
            return (
                "<urlset><url><loc>https://ex.com/a</loc></url>"
                "<url><loc>https://ex.com/b</loc></url></urlset>"
            )
        return None

    async def distinct_renderer(url):
        return (
            200,
            f"<html><head><title>{url}</title></head><body><article>"
            f"<p>Unique readable content for {url} with several words to extract.</p>"
            f"</article></body></html>",
        )

    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value=_EXPECTED)),
        patch("app.auditor.pipeline._active_source_cap", return_value=1),  # plan cap = 1
    ):
        await pipeline.run_audit(
            db, report.id, page_renderer=distinct_renderer, fetch_text=fetch_ab, resume=False
        )

    active = (
        db.query(AuditEntry)
        .filter(
            AuditEntry.audit_report_id == report.id,
            AuditEntry.status == AuditEntryStatus.ACTIVE,
        )
        .count()
    )
    assert (
        active == 0
    ), f"uploaded file already fills the cap → no budget for restored actives; got {active}"


# ---------------------------------------------------------------------------
# Guard rejection must not be reported as our renderer breaking.
#
# Regression: a real audit of a live, healthy site failed with RENDER_FAILED
# ("Something went wrong on our end while reading your site. Please try again.")
# because DNS briefly failed inside the worker. resolve_and_validate_host turns
# a gaierror into the same rejection as a malicious URL, so guard_url said no,
# seed_urls returned [], the crawl never ran, and classify_scan_outcome saw a
# missing root page — its RENDER_FAILED branch. The same path hits any customer
# whose domain is typo'd or not yet live, telling them to retry forever.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_root_reports_unreachable_not_render_failed(db):
    from app.db.models.audit import ScanQuality
    from app.utils.network_security import HostResolutionError

    job, report = _make_job_and_report(db, scope=AuditScope.PUBLIC)
    with patch(
        "app.auditor.discovery.resolve_and_validate_host",
        side_effect=HostResolutionError("Could not resolve hostname: ex.com"),
    ):
        result = await pipeline.run_audit(db, report.id, fetch_text=_fake_fetch,
                                          page_renderer=_fake_renderer)

    assert result.status == AuditStatus.FAILED
    assert result.scan_quality == ScanQuality.UNREACHABLE
    # The user must not be told this was our fault, nor to "try again".
    assert "our end" not in (result.scan_quality_reason or "").lower()
    assert result.pages_scanned == 0
