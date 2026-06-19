"""Tests for AuditService.import_entries + plan-gating + idempotency."""

import uuid
from unittest.mock import patch

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


def test_import_is_idempotent(db):
    _tid, report = _setup(db, max_sources=2)
    svc = AuditService(db)
    svc.preselect_entries(report.id)
    db.flush()
    with patch("app.auditor.service.enqueue_document_ingestion"):
        first = svc.import_entries(report.id)
        second = svc.import_entries(report.id)  # re-run
    db.flush()
    assert len(first) == 2
    assert second == []  # nothing new imported
    assert (
        db.query(KnowledgeSource)
        .filter(KnowledgeSource.tenant_id == report.tenant_id)
        .count()
        == 2
    )


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
