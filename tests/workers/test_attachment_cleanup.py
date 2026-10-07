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


async def test_cleanup_continues_when_destroy_raises(db, monkeypatch):
    import app.workers.attachment_cleanup as mod
    from app.workers.attachment_cleanup import cleanup_orphan_attachments

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    s = _session(db)
    old_at = datetime.now(UTC) - timedelta(hours=26)
    _att(db, s, "chat/a", status=AttachmentStatus.UPLOADED, created_at=old_at)
    _att(db, s, "chat/b", status=AttachmentStatus.UPLOADED, created_at=old_at)

    with patch("app.workers.attachment_cleanup.cloudinary_manager.destroy") as d:
        d.side_effect = RuntimeError("boom")
        n = await cleanup_orphan_attachments({})

    assert n == 2
    assert d.call_count == 2
