"""Agent image upload + agent-message attachments + fan-out (takeover)."""

import io
from datetime import datetime, UTC
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from PIL import Image
from sqlalchemy import select

from app.api.deps import get_current_user
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    SessionStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole
from app.main import app
from app.schemas.user import UserResponse

CM = "app.api.v1.endpoints.chat.cloudinary_manager"
MOD = "app.api.v1.endpoints.chat.moderate_image"
WS = "app.api.v1.endpoints.chat.connection_manager"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 3), "red").save(buf, format="PNG")
    return buf.getvalue()


def _seed(db, *, allow_customer_upload: bool = False):
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"t_{prefix}",
        display_name=f"T {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
        widget_allow_file_upload=allow_customer_upload,
    )
    db.add(tenant)
    db.flush()
    session = ConversationSession(
        tenant_id=tenant.id, user_identifier=f"v-{prefix}", status=SessionStatus.ACTIVE
    )
    db.add(session)
    db.commit()
    return session


def _login_as(tenant_id: UUID, role: UserRole = UserRole.SUPPORT_AGENT) -> UserResponse:
    now = datetime.now(UTC)
    user = UserResponse(
        id=uuid4(),
        tenant_id=tenant_id,
        email="agent@example.com",
        full_name="Agent Smith",
        role=role,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _upload(client, session_id):
    return client.post(
        f"/api/v1/chat/sessions/{session_id}/upload",
        files={"file": ("a.png", _png(), "image/png")},
    )


def test_agent_upload_creates_outbound_attachment(client, db):
    session = _seed(db)  # customer uploads disabled: must not gate agents
    sid, tid = session.id, session.tenant_id
    _login_as(tid)
    with patch(CM) as cm, patch(MOD, new=AsyncMock(return_value=(True, None))):
        cm.validate_image.return_value = (True, None)
        cm.upload_chat_image.return_value = ("https://c/a.jpg", "chat/a", None)
        r = _upload(client, sid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == "https://c/a.jpg"
    assert body["width"] == 4 and body["height"] == 3
    row = db.execute(
        select(MessageAttachment).where(MessageAttachment.id == UUID(body["attachment_id"]))
    ).scalar_one()
    assert row.direction == AttachmentDirection.OUTBOUND
    assert row.status == AttachmentStatus.UPLOADED
    assert row.moderation_status == AttachmentModeration.PASSED
    assert row.message_id is None


def test_agent_upload_other_tenant_forbidden(client, db):
    session = _seed(db)
    other = _seed(db)
    sid = session.id
    _login_as(other.tenant_id)
    with patch(CM) as cm, patch(MOD, new=AsyncMock(return_value=(True, None))):
        cm.validate_image.return_value = (True, None)
        cm.upload_chat_image.return_value = ("https://c/a.jpg", "chat/a", None)
        r = _upload(client, sid)
    assert r.status_code == 403
    assert db.execute(select(MessageAttachment)).scalars().all() == []


def test_agent_message_links_attachments_and_fans_out(client, db):
    session = _seed(db)
    sid, tid = session.id, session.tenant_id
    _login_as(tid)
    with patch(CM) as cm, patch(MOD, new=AsyncMock(return_value=(True, None))):
        cm.validate_image.return_value = (True, None)
        cm.upload_chat_image.return_value = ("https://c/a.jpg", "chat/a", None)
        att_id = _upload(client, sid).json()["attachment_id"]

    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = client.post(
            f"/api/v1/chat/sessions/{sid}/agent-message",
            json={"message": "here you go", "attachment_ids": [att_id]},
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert [a["id"] for a in body["attachments"]] == [att_id]
    assert body["attachments"][0]["url"] == "https://c/a.jpg"

    row = db.execute(
        select(MessageAttachment).where(MessageAttachment.id == UUID(att_id))
    ).scalar_one()
    db.refresh(row)
    assert row.status == AttachmentStatus.LINKED
    assert str(row.message_id) == body["id"]
    assert db.get(Message, UUID(body["id"])) is not None

    sent = ws.fanout_event.await_args.kwargs["message"]
    assert sent["attachments"] == [
        {"id": att_id, "url": "https://c/a.jpg", "content_type": "image/png"}
    ]


def test_agent_message_image_only_and_bad_attachment(client, db):
    session = _seed(db)
    sid, tid = session.id, session.tenant_id
    _login_as(tid)
    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = client.post(
            f"/api/v1/chat/sessions/{sid}/agent-message",
            json={"attachment_ids": [str(uuid4())]},
        )
    assert r.status_code == 400
    assert db.execute(select(Message).where(Message.session_id == sid)).scalars().all() == []
    ws.fanout_event.assert_not_awaited()
