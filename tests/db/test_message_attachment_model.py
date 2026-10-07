"""MessageAttachment model: insert + tenant scoping."""

import uuid

import pytest

from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
)
from app.db.models.tenant import Tenant


@pytest.fixture
def seed_session(db):
    tenant = Tenant(
        name=f"att_{uuid.uuid4().hex[:8]}",
        display_name="Attachment Co",
        api_key_hash="hash_att",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    session = ConversationSession(tenant_id=tenant.id, user_identifier="visitor:att")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_message_attachment_insert_and_tenant_scope(db, seed_session):
    a = MessageAttachment(
        session_id=seed_session.id,
        tenant_id=seed_session.tenant_id,
        direction=AttachmentDirection.INBOUND,
        cloudinary_public_id="luran/chat/x",
        url="https://res.cloudinary.com/x.jpg",
        content_type="image/jpeg",
        bytes=1234,
        moderation_status=AttachmentModeration.PASSED,
        status=AttachmentStatus.UPLOADED,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    assert a.id and a.message_id is None and a.tenant_id == seed_session.tenant_id
    assert a.url == "https://res.cloudinary.com/x.jpg"
    assert a.created_at is not None
