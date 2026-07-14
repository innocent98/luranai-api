"""check_knowledge_source_limit must count in-flight (PROCESSING) sources.

Audit imports create KnowledgeSource rows at status=PROCESSING and flip them to
ACTIVE once the ingestion worker runs. If the upload gate counts ACTIVE only, a
tenant can import findings (PROCESSING) and THEN upload files past the cap before
the imports finish — the same overflow the auditor fix closed, in reverse order.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.db.models.knowledge import KnowledgeSource, SourceStatus, SourceType
from app.db.models.tenant import TenantCapabilities
from app.services.knowledge_service import KnowledgeService


def _cap(db, tid, max_sources):
    db.add(
        TenantCapabilities(
            tenant_id=tid,
            can_upload_knowledge=True,
            max_knowledge_sources=max_sources,
            can_use_url_sources=True,
        )
    )
    db.flush()


def _source(db, tid, i, status):
    db.add(
        KnowledgeSource(
            tenant_id=tid,
            name=f"s{i}",
            source_type=SourceType.OTHER,
            source_category="file",
            content_hash=f"{i:064d}",
            version=1,
            status=status,
        )
    )
    db.flush()


def test_processing_source_counts_toward_limit(db):
    """A single in-flight (PROCESSING) source at cap=1 must block a new upload."""
    tid = uuid.uuid4()
    _cap(db, tid, max_sources=1)
    _source(db, tid, 0, SourceStatus.PROCESSING)

    with pytest.raises(HTTPException) as exc:
        KnowledgeService(db).check_knowledge_source_limit(tid, "file")
    assert exc.value.status_code == 403


def test_active_source_still_counts_toward_limit(db):
    """Regression anchor: an ACTIVE source at cap=1 still blocks (unchanged)."""
    tid = uuid.uuid4()
    _cap(db, tid, max_sources=1)
    _source(db, tid, 0, SourceStatus.ACTIVE)

    with pytest.raises(HTTPException) as exc:
        KnowledgeService(db).check_knowledge_source_limit(tid, "file")
    assert exc.value.status_code == 403


def test_inactive_source_does_not_count(db):
    """An INACTIVE (soft-deleted) source frees the slot — upload allowed."""
    tid = uuid.uuid4()
    _cap(db, tid, max_sources=1)
    _source(db, tid, 0, SourceStatus.INACTIVE)

    # Should NOT raise — the only source is inactive.
    KnowledgeService(db).check_knowledge_source_limit(tid, "file")
