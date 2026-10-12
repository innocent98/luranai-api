"""MessageAttachment defaults for kind and expires_at."""

from datetime import UTC, datetime, timedelta

from app.db.models.conversation import AttachmentKind, AttachmentStatus
from tests.api.v1 import test_chat_image_send as img


def test_new_attachment_defaults_to_image_and_30_day_expiry(db):
    sid, tid = img._session(db)
    before = datetime.now(UTC)
    aid = img._att(db, sid, tid)
    from app.db.models.conversation import MessageAttachment

    row = db.get(MessageAttachment, aid)
    assert row.kind == AttachmentKind.IMAGE
    assert row.status == AttachmentStatus.UPLOADED
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    assert before + timedelta(days=30) - timedelta(minutes=1) <= exp
    assert exp <= datetime.now(UTC) + timedelta(days=30, minutes=1)
