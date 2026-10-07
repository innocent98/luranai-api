"""Public customer image upload endpoint (POST /api/v1/chat/upload)."""

import io
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from PIL import Image
from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
    SessionStatus,
)
from app.db.models.tenant import Tenant

CM = "app.api.v1.endpoints.chat.cloudinary_manager"
MOD = "app.api.v1.endpoints.chat.moderate_image"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 3), "red").save(buf, format="PNG")
    return buf.getvalue()


def _seed(db, *, allow: bool):
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"t_{prefix}",
        display_name=f"T {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
        widget_allow_file_upload=allow,
    )
    db.add(tenant)
    db.flush()
    session = ConversationSession(
        tenant_id=tenant.id, user_identifier=f"v-{prefix}", status=SessionStatus.ACTIVE
    )
    db.add(session)
    db.commit()
    return session


def _post(client, session, content=None, ctype="image/png"):
    return client.post(
        "/api/v1/chat/upload",
        data={"session_id": str(session.id)},
        files={"file": ("a.png", content if content is not None else _png(), ctype)},
    )


def _rows(db):
    return db.execute(select(MessageAttachment)).scalars().all()


def test_clean_upload_creates_unlinked_attachment(client, db):
    session = _seed(db, allow=True)
    tenant_id = session.tenant_id  # request closes the shared db session
    with (
        patch(CM) as cm,
        patch(MOD, new=AsyncMock(return_value=(True, None))),
    ):
        cm.validate_image.return_value = (True, None)
        cm.upload_chat_image.return_value = ("https://c/x.jpg", "chat/x", None)
        r = _post(client, session)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == "https://c/x.jpg"
    assert body["content_type"] == "image/png"
    assert body["width"] == 4 and body["height"] == 3 and body["bytes"] > 0
    row = db.execute(
        select(MessageAttachment).where(MessageAttachment.id == UUID(body["attachment_id"]))
    ).scalar_one()
    assert row.message_id is None
    assert row.status == AttachmentStatus.UPLOADED
    assert row.direction == AttachmentDirection.INBOUND
    assert row.moderation_status == AttachmentModeration.PASSED
    assert row.tenant_id == tenant_id


def test_gate_off_returns_403(client, db):
    session = _seed(db, allow=False)
    with patch(CM) as cm, patch(MOD, new=AsyncMock()) as mod:
        r = _post(client, session)
    assert r.status_code == 403
    mod.assert_not_called()
    cm.upload_chat_image.assert_not_called()
    assert _rows(db) == []


def test_unknown_session_404(client, db):
    class S:
        id = uuid4()

    assert _post(client, S()).status_code == 404


def test_invalid_type_415_no_moderation_or_upload(client, db):
    session = _seed(db, allow=True)
    with patch(CM) as cm, patch(MOD, new=AsyncMock()) as mod:
        cm.validate_image.return_value = (False, "Image format 'BMP' not allowed")
        r = _post(client, session)
    assert r.status_code == 415
    mod.assert_not_called()
    cm.upload_chat_image.assert_not_called()
    assert _rows(db) == []


def test_oversized_413_bounded_read_no_validation_moderation_or_upload(client, db):
    session = _seed(db, allow=True)
    big = b"\x00" * (10 * 1024 * 1024 + 1)
    with patch(CM) as cm, patch(MOD, new=AsyncMock()) as mod:
        r = _post(client, session, content=big)
    assert r.status_code == 413
    cm.validate_image.assert_not_called()
    mod.assert_not_called()
    cm.upload_chat_image.assert_not_called()
    assert _rows(db) == []


def test_moderation_reject_422_nothing_stored(client, db):
    session = _seed(db, allow=True)
    with (
        patch(CM) as cm,
        patch(MOD, new=AsyncMock(return_value=(False, "Image not allowed"))),
    ):
        cm.validate_image.return_value = (True, None)
        r = _post(client, session)
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "image_rejected", "reason": "Image not allowed"}
    cm.upload_chat_image.assert_not_called()
    assert _rows(db) == []


def test_moderation_error_fails_closed_422(client, db):
    session = _seed(db, allow=True)
    with patch(CM) as cm, patch(MOD, new=AsyncMock(side_effect=RuntimeError("boom"))):
        cm.validate_image.return_value = (True, None)
        r = _post(client, session)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "image_rejected"
    cm.upload_chat_image.assert_not_called()
    assert _rows(db) == []


def test_cloudinary_error_502_no_row(client, db):
    session = _seed(db, allow=True)
    with patch(CM) as cm, patch(MOD, new=AsyncMock(return_value=(True, None))):
        cm.validate_image.return_value = (True, None)
        cm.upload_chat_image.return_value = (None, None, "Upload failed")
        r = _post(client, session)
    assert r.status_code == 502
    assert _rows(db) == []
