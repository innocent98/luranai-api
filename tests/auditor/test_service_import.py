"""Tests for AuditService.import_entries + plan-gating + idempotency."""

import uuid
from unittest.mock import AsyncMock, patch

from app.auditor.service import AuditService
from app.db.models.audit import (
    AuditEntry,
    AuditEntryKind,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
    AuditStatus,
)
from app.db.models.knowledge import KnowledgeSource
from app.db.models.tenant import TenantCapabilities


def _setup(db, max_sources):
    tid = uuid.uuid4()
    db.add(TenantCapabilities(tenant_id=tid, max_knowledge_sources=max_sources))
    report = AuditReport(
        tenant_id=tid,
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.DASHBOARD,
        status=AuditStatus.COMPLETED,
    )
    db.add(report)
    db.flush()
    for i in range(3):
        db.add(
            AuditEntry(
                audit_report_id=report.id,
                tenant_id=tid,
                url=f"https://ex.com/{i}",
                title=f"P{i}",
                category="c",
                kind=AuditEntryKind.FAQ,
                content=f"content {i}",
                content_hash=("%064d" % i),
                importance_rank=i + 1,
                status=AuditEntryStatus.PROPOSED,
            )
        )
    db.flush()
    return tid, report


def test_preselect_respects_max_knowledge_sources(db):
    _tid, report = _setup(db, max_sources=2)
    active, locked = AuditService(db).preselect_entries(report.id)
    db.flush()
    assert len(active) == 2 and len(locked) == 1
    assert active[0].importance_rank <= active[1].importance_rank
    assert all(e.status == AuditEntryStatus.ACTIVE for e in active)
    assert locked[0].status == AuditEntryStatus.LOCKED


async def test_import_is_idempotent(db):
    _tid, report = _setup(db, max_sources=2)
    svc = AuditService(db)
    svc.preselect_entries(report.id)
    db.flush()
    with patch("app.auditor.service.enqueue_document_ingestion", new_callable=AsyncMock):
        first = await svc.import_entries(report.id)
        second = await svc.import_entries(report.id)  # re-run
    db.flush()
    assert len(first) == 2
    assert second == []  # nothing new imported
    assert (
        db.query(KnowledgeSource)
        .filter(KnowledgeSource.tenant_id == report.tenant_id)
        .count()
        == 2
    )


async def test_preselect_preserves_manual_swap_on_reimport(db):
    """preselect_entries must not revert tenant's manual ACTIVE/LOCKED swaps.

    Scenario (limit=2, 3 entries by rank):
      - Initial preselect: rank-1 → ACTIVE, rank-2 → ACTIVE, rank-3 → LOCKED
      - Tenant swaps: rank-3 → ACTIVE, rank-1 → LOCKED  (still 2 active, within limit)
      - Second preselect (as called before import): must leave the swap intact
      - import_entries imports rank-2 and rank-3 (the two ACTIVE entries after swap)
        and does NOT import rank-1 (LOCKED by tenant)
    """
    _tid, report = _setup(db, max_sources=2)
    svc = AuditService(db)

    # Initial preselect: rank-1 and rank-2 → ACTIVE; rank-3 → LOCKED
    svc.preselect_entries(report.id)
    db.flush()

    entries = (
        db.query(AuditEntry)
        .filter(AuditEntry.audit_report_id == report.id)
        .order_by(AuditEntry.importance_rank.asc())
        .all()
    )
    rank1, rank2, rank3 = entries
    assert rank1.status == AuditEntryStatus.ACTIVE
    assert rank2.status == AuditEntryStatus.ACTIVE
    assert rank3.status == AuditEntryStatus.LOCKED

    # Tenant manually swaps: lock rank-1, activate rank-3
    rank1.status = AuditEntryStatus.LOCKED
    rank3.status = AuditEntryStatus.ACTIVE
    db.flush()

    # Call preselect again (as import endpoint does) — must be a no-op on curated entries
    svc.preselect_entries(report.id)
    db.flush()

    db.refresh(rank1)
    db.refresh(rank2)
    db.refresh(rank3)
    assert rank1.status == AuditEntryStatus.LOCKED, "rank-1 should still be LOCKED after re-preselect"
    assert rank2.status == AuditEntryStatus.ACTIVE, "rank-2 should remain ACTIVE"
    assert rank3.status == AuditEntryStatus.ACTIVE, "rank-3 should remain ACTIVE (tenant activated)"

    # Import: only rank-2 and rank-3 (the ACTIVE entries) should be imported
    with patch("app.auditor.service.enqueue_document_ingestion", new_callable=AsyncMock):
        imported_ids = await svc.import_entries(report.id)

    assert rank1.id not in imported_ids, "Tenant-locked rank-1 must NOT be imported"
    assert rank2.id in imported_ids, "rank-2 must be imported"
    assert rank3.id in imported_ids, "Tenant-activated rank-3 must be imported"


def test_upgrade_unlock_counts_newly_eligible(db):
    tid, report = _setup(db, max_sources=2)
    svc = AuditService(db)
    svc.preselect_entries(report.id)
    db.flush()
    cap = (
        db.query(TenantCapabilities)
        .filter(TenantCapabilities.tenant_id == tid)
        .one()
    )
    cap.max_knowledge_sources = 3
    db.flush()
    assert svc.unlock_after_upgrade(report.id) == 1


async def test_import_worker_path_single_source(db):
    """Worker must activate the pre-created source, not spawn a second one.

    Drives process_document_ingestion directly (the path existing tests missed)
    and asserts:
      (a) exactly ONE KnowledgeSource exists for the entry
      (b) it has KnowledgeChunk rows
      (c) it ends ACTIVE (not stuck PROCESSING)
      (d) AuditEntry.knowledge_source_id points to it and status == IMPORTED
    """
    from unittest.mock import AsyncMock, patch

    from app.auditor.service import AuditService
    from app.db.models.audit import AuditEntryKind, AuditReport, AuditScope, AuditStatus
    from app.db.models.job import ProcessingJob
    from app.db.models.knowledge import KnowledgeChunk, KnowledgeSource, SourceStatus
    from app.workers.document_processor import process_document_ingestion

    # ---- setup: tenant + report + entry ----
    tid = uuid.uuid4()
    db.add(TenantCapabilities(tenant_id=tid, max_knowledge_sources=5))
    report = AuditReport(
        tenant_id=tid,
        url="https://worker-test.com",
        domain="worker-test.com",
        scope=AuditScope.DASHBOARD,
        status=AuditStatus.COMPLETED,
    )
    db.add(report)
    db.flush()
    db.add(
        AuditEntry(
            audit_report_id=report.id,
            tenant_id=tid,
            url="https://worker-test.com/page",
            title="Worker Test Page",
            category="faq",
            kind=AuditEntryKind.FAQ,
            content=(
                "This is a test page for the worker path integration test. "
                "It contains enough words so that the text chunker will produce "
                "at least one chunk, which requires roughly sixty tokens or more. "
                "The chunker default minimum is sixty tokens so this paragraph "
                "must be long enough to exceed that threshold. We are adding "
                "additional sentences here to make absolutely certain the minimum "
                "chunk size requirement is satisfied by this content string."
            ),
            content_hash="%064d" % 99,
            importance_rank=1,
            status=AuditEntryStatus.PROPOSED,
        )
    )
    db.flush()

    svc = AuditService(db)
    svc.preselect_entries(report.id)
    db.flush()

    # import_entries creates 1 KnowledgeSource (PROCESSING) and enqueues job
    with patch("app.auditor.service.enqueue_document_ingestion", new_callable=AsyncMock):
        imported = await svc.import_entries(report.id)
    assert len(imported) == 1

    # Exactly 1 source must exist, still PROCESSING
    sources_before = (
        db.query(KnowledgeSource)
        .filter(KnowledgeSource.tenant_id == tid)
        .all()
    )
    assert len(sources_before) == 1
    pre_source_id = sources_before[0].id
    assert sources_before[0].status == SourceStatus.PROCESSING

    # Fetch the ProcessingJob the service created
    job = db.query(ProcessingJob).filter(ProcessingJob.tenant_id == tid).one()

    fake_embedding = [0.1] * 1536

    # Patch db.close to a no-op so the worker's finally block doesn't kill
    # the test session, then redirect SessionLocal() to return our test db.
    with (
        patch.object(db, "close", lambda: None),
        patch(
            "app.workers.document_processor.SessionLocal",
            return_value=db,
        ),
        patch(
            "app.workers.document_processor.embedding_service.generate_embedding",
            new_callable=AsyncMock,
            return_value=fake_embedding,
        ),
        patch(
            "app.utils.qdrant_client.qdrant_manager.ensure_collection",
            return_value=True,
        ),
        patch(
            "app.utils.qdrant_client.qdrant_manager.upsert_vector",
            return_value=None,
        ),
        patch(
            "app.workers.document_processor.send_job_completion_email",
            new_callable=AsyncMock,
        ),
        patch(
            "app.workers.document_processor.broadcast_job_status",
            new_callable=AsyncMock,
        ),
    ):
        result = await process_document_ingestion({}, str(job.id))

    assert result["success"] is True, f"Worker failed: {result}"

    # (a) exactly ONE KnowledgeSource
    sources_after = (
        db.query(KnowledgeSource)
        .filter(KnowledgeSource.tenant_id == tid)
        .all()
    )
    assert len(sources_after) == 1, (
        f"Expected 1 KnowledgeSource, got {len(sources_after)}"
    )
    the_source = sources_after[0]
    assert the_source.id == pre_source_id  # same source, not a new one

    # (b) has KnowledgeChunk rows
    chunks = (
        db.query(KnowledgeChunk)
        .filter(KnowledgeChunk.source_id == the_source.id)
        .all()
    )
    assert len(chunks) > 0, "Expected at least one chunk"

    # (c) ends ACTIVE
    db.refresh(the_source)
    assert the_source.status == SourceStatus.ACTIVE, (
        f"Expected ACTIVE, got {the_source.status}"
    )

    # (d) AuditEntry.knowledge_source_id points to it, status == IMPORTED
    entry = (
        db.query(AuditEntry)
        .filter(AuditEntry.audit_report_id == report.id)
        .one()
    )
    assert entry.knowledge_source_id == the_source.id
    assert entry.status == AuditEntryStatus.IMPORTED
