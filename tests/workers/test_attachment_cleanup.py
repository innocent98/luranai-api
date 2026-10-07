"""Orphan chat-attachment cleanup cron."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)
from app.db.models.tenant import Tenant
from app.services.conversation_service import ConversationService


class _NoCloseSession:
    """Proxy around the shared test Session that swallows close()."""

    def __init__(self, session):
        self._session = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def _session(db):
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"t_{prefix}",
        display_name=f"T {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    s = ConversationSession(
        tenant_id=tenant.id, user_identifier=f"v-{prefix}", status=SessionStatus.ACTIVE
    )
    db.add(s)
    db.commit()
    return s


def _att(db, session, public_id, **kw):
    a = MessageAttachment(
        session_id=session.id,
        tenant_id=session.tenant_id,
        direction=AttachmentDirection.INBOUND,
        cloudinary_public_id=public_id,
        url="https://c/x.jpg",
        content_type="image/png",
        width=4,
        height=3,
        bytes=10,
        moderation_status=AttachmentModeration.PASSED,
        **kw,
    )
    db.add(a)
    db.commit()
    return a


async def test_cleanup_removes_old_unlinked_only(db, monkeypatch):
    import app.workers.attachment_cleanup as mod
    from app.workers.attachment_cleanup import cleanup_orphan_attachments

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    now = datetime.now(UTC)
    old = _att(
        db,
        s,
        "chat/old",
        status=AttachmentStatus.UPLOADED,
        created_at=now - timedelta(hours=25),
    )
    recent = _att(db, s, "chat/recent", status=AttachmentStatus.UPLOADED, created_at=now)
    msg = ConversationService(db).add_message(s.id, MessageRole.USER, "hi")
    linked = _att(
        db,
        s,
        "chat/linked",
        status=AttachmentStatus.LINKED,
        message_id=msg.id,
        created_at=now - timedelta(hours=30),
    )
    old_id, recent_id, linked_id = old.id, recent.id, linked.id

    with patch("app.workers.attachment_cleanup.cloudinary_manager.destroy") as d:
        d.return_value = (True, None)
        n = await cleanup_orphan_attachments({})

    assert n == 1
    d.assert_called_once_with("chat/old")
    assert db.query(MessageAttachment).filter_by(id=old_id).count() == 0
    assert db.query(MessageAttachment).filter_by(id=recent_id).count() == 1
    assert db.query(MessageAttachment).filter_by(id=linked_id).count() == 1


async def test_destroy_failure_keeps_row_and_not_counted(db, monkeypatch):
    import app.workers.attachment_cleanup as mod
    from app.workers.attachment_cleanup import cleanup_orphan_attachments

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    old_at = datetime.now(UTC) - timedelta(hours=26)
    bad = _att(db, s, "chat/bad", status=AttachmentStatus.UPLOADED, created_at=old_at)
    good = _att(db, s, "chat/good", status=AttachmentStatus.UPLOADED, created_at=old_at)
    bad_id, good_id = bad.id, good.id

    def fake_destroy(public_id):
        return (False, "x") if public_id == "chat/bad" else (True, None)

    warnings = []
    sink = mod.logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        with patch("app.workers.attachment_cleanup.cloudinary_manager.destroy") as d:
            d.side_effect = fake_destroy
            n = await cleanup_orphan_attachments({})
    finally:
        mod.logger.remove(sink)

    assert n == 1
    assert db.query(MessageAttachment).filter_by(id=bad_id).count() == 1
    assert db.query(MessageAttachment).filter_by(id=good_id).count() == 0
    assert any("chat/bad" in w and str(bad_id) in w for w in warnings)


async def test_cleanup_keeps_rows_when_destroy_raises(db, monkeypatch):
    import app.workers.attachment_cleanup as mod
    from app.workers.attachment_cleanup import cleanup_orphan_attachments

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    old_at = datetime.now(UTC) - timedelta(hours=26)
    a = _att(db, s, "chat/a", status=AttachmentStatus.UPLOADED, created_at=old_at)
    b = _att(db, s, "chat/b", status=AttachmentStatus.UPLOADED, created_at=old_at)
    ids = [a.id, b.id]

    with patch("app.workers.attachment_cleanup.cloudinary_manager.destroy") as d:
        d.side_effect = RuntimeError("boom")
        n = await cleanup_orphan_attachments({})

    assert n == 0
    assert d.call_count == 2
    assert db.query(MessageAttachment).filter(MessageAttachment.id.in_(ids)).count() == 2
