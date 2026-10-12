"""AttachmentOut / attachment_frames carry kind, video fields and expired."""

from app.db.models.conversation import (
    AttachmentKind,
    AttachmentStatus,
    MessageAttachment,
)
from app.schemas.conversation import AttachmentOut
from app.services.conversation_service import attachment_frames
from tests.api.v1 import test_chat_image_send as img


def _row(db, **kw):
    sid, tid = img._session(db)
    aid = img._att(db, sid, tid)
    row = db.get(MessageAttachment, aid)
    for k, v in kw.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


def test_image_defaults(db):
    out = AttachmentOut.model_validate(_row(db))
    assert out.kind == "image"
    assert out.expired is False
    assert out.duration_ms is None and out.poster_url is None


def test_video_fields(db):
    row = _row(
        db,
        kind=AttachmentKind.VIDEO,
        duration_ms=42000,
        poster_url="https://c/p.jpg",
        content_type="video/mp4",
    )
    out = AttachmentOut.model_validate(row)
    assert (out.kind, out.duration_ms, out.poster_url) == ("video", 42000, "https://c/p.jpg")
    frame = attachment_frames([row])[0]
    assert frame["kind"] == "video" and frame["duration_ms"] == 42000
    assert frame["poster_url"] == "https://c/p.jpg" and frame["expired"] is False


def test_expired_has_null_urls_everywhere(db):
    row = _row(db, status=AttachmentStatus.EXPIRED, url=None, poster_url=None)
    out = AttachmentOut.model_validate(row)
    assert out.expired is True and out.url is None and out.poster_url is None
    frame = attachment_frames([row])[0]
    assert frame["expired"] is True and frame["url"] is None
