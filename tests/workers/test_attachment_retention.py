"""Daily attachment-retention job."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from app.db.models.conversation import (
    AttachmentKind,
    AttachmentStatus,
    MessageAttachment,
    MessageRole,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.services.conversation_service import ConversationService
from tests.workers.test_attachment_cleanup import _att, _NoCloseSession, _session

DESTROY = "app.workers.attachment_retention.cloudinary_manager.destroy"


def _linked(db, s, pid, *, expires_at, kind=AttachmentKind.IMAGE):
    msg = ConversationService(db).add_message(s.id, MessageRole.USER, "hi")
    return _att(
        db,
        s,
        pid,
        status=AttachmentStatus.LINKED,
        message_id=msg.id,
        expires_at=expires_at,
        kind=kind,
    )


def _ticket(db, s, status):
    t = Ticket(
        tenant_id=s.tenant_id,
        session_id=s.id,
        ticket_number=f"T-{uuid4().hex[:6]}",
        priority=TicketPriority.MEDIUM,
        status=status,
        summary="x",
    )
    db.add(t)
    db.commit()


async def _run(db, monkeypatch, **destroy_kw):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    with patch(DESTROY, **destroy_kw) as d:
        n = await mod.expire_attachments({})
    return n, d


async def test_expires_due_rows_keeps_row_nulls_urls(db, monkeypatch):
    s = _session(db)
    now = datetime.now(UTC)
    due = _linked(db, s, "a/due", expires_at=now - timedelta(hours=1), kind=AttachmentKind.VIDEO)
    future = _linked(db, s, "a/future", expires_at=now + timedelta(days=1))
    due_id, future_id = due.id, future.id
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1
    d.assert_called_once_with("a/due", resource_type="video")
    db.expire_all()
    row = db.get(MessageAttachment, due_id)
    assert row.status == AttachmentStatus.EXPIRED and row.url is None and row.poster_url is None
    assert db.get(MessageAttachment, future_id).status == AttachmentStatus.LINKED


async def test_active_ticket_defers_until_ceiling(db, monkeypatch):
    s = _session(db)
    _ticket(db, s, TicketStatus.IN_PROGRESS)
    now = datetime.now(UTC)
    recent = _linked(db, s, "a/recent", expires_at=now - timedelta(days=1))
    stale = _linked(db, s, "a/stale", expires_at=now - timedelta(days=8))
    recent_id, stale_id = recent.id, stale.id
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1
    d.assert_called_once_with("a/stale", resource_type="image")
    db.expire_all()
    assert db.get(MessageAttachment, recent_id).status == AttachmentStatus.LINKED
    assert db.get(MessageAttachment, stale_id).status == AttachmentStatus.EXPIRED


async def test_resolved_ticket_does_not_defer(db, monkeypatch):
    s = _session(db)
    _ticket(db, s, TicketStatus.RESOLVED)
    _linked(db, s, "a/x", expires_at=datetime.now(UTC) - timedelta(hours=1))
    n, _ = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 1


async def test_failing_destroy_is_isolated_and_terminates(db, monkeypatch):
    s = _session(db)
    past = datetime.now(UTC) - timedelta(hours=1)
    bad = _linked(db, s, "a/bad", expires_at=past)
    good = _linked(db, s, "a/good", expires_at=past)
    bad_id, good_id = bad.id, good.id

    def fake(public_id, *, resource_type):
        return (False, "boom") if public_id == "a/bad" else (True, None)

    n, d = await _run(db, monkeypatch, side_effect=fake)
    assert n == 1
    assert d.call_count == 2  # bad tried once, not retried in a loop
    db.expire_all()
    assert db.get(MessageAttachment, bad_id).status == AttachmentStatus.LINKED
    assert db.get(MessageAttachment, good_id).status == AttachmentStatus.EXPIRED


async def test_kill_switch(db, monkeypatch):
    import app.workers.attachment_retention as mod

    monkeypatch.setattr(mod.settings, "ATTACHMENT_RETENTION_ENABLED", False)
    s = _session(db)
    _linked(db, s, "a/x", expires_at=datetime.now(UTC) - timedelta(hours=1))
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 0
    d.assert_not_called()


async def test_uploaded_rows_ignored(db, monkeypatch):
    s = _session(db)
    _att(
        db,
        s,
        "a/up",
        status=AttachmentStatus.UPLOADED,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    n, d = await _run(db, monkeypatch, return_value=(True, None))
    assert n == 0  # unsent uploads belong to cleanup_orphan_attachments
