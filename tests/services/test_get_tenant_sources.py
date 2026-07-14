"""get_tenant_sources must hide soft-deleted (INACTIVE) sources by default.

Deleting a KB soft-deletes it (status=INACTIVE); version-supersede does the same.
INACTIVE rows are tombstones with no user action attached, so the default listing
must exclude them — otherwise the dashboard shows "Inactive" rows users can't undo.
An explicit status filter still returns them (admin/debug).
"""

import uuid

from app.db.models.knowledge import KnowledgeSource, SourceStatus, SourceType
from app.services.knowledge_service import KnowledgeService


def _source(db, tid, name, status):
    src = KnowledgeSource(
        tenant_id=tid,
        name=name,
        source_type=SourceType.OTHER,
        source_category="file",
        content_hash=f"{abs(hash(name)):064d}"[:64],
        version=1,
        status=status,
    )
    db.add(src)
    db.flush()
    return src


def test_default_listing_excludes_inactive(db):
    tid = uuid.uuid4()
    _source(db, tid, "live", SourceStatus.ACTIVE)
    _source(db, tid, "deleted", SourceStatus.INACTIVE)

    names = {s.name for s in KnowledgeService(db).get_tenant_sources(tid)}
    assert names == {"live"}, f"default listing must hide INACTIVE, got {names}"


def test_processing_and_failed_still_listed_by_default(db):
    tid = uuid.uuid4()
    _source(db, tid, "ingesting", SourceStatus.PROCESSING)
    _source(db, tid, "broken", SourceStatus.FAILED)

    names = {s.name for s in KnowledgeService(db).get_tenant_sources(tid)}
    assert names == {
        "ingesting",
        "broken",
    }, f"only INACTIVE is hidden; PROCESSING/FAILED stay visible, got {names}"


def test_explicit_inactive_filter_still_returns_them(db):
    tid = uuid.uuid4()
    _source(db, tid, "deleted", SourceStatus.INACTIVE)

    names = {
        s.name for s in KnowledgeService(db).get_tenant_sources(tid, status=SourceStatus.INACTIVE)
    }
    assert names == {"deleted"}, f"explicit INACTIVE filter must still work, got {names}"
