"""Video sign/register endpoints. The Cloudinary SDK surface is mocked."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentKind,
    AttachmentModeration,
    AttachmentStatus,
    MessageAttachment,
)
from app.db.models.tenant import Tenant
from tests.api.v1 import test_chat_image_send as img

CM = "app.api.v1.endpoints.chat_video.cloudinary_manager"


def _seed(db, *, allowed=True):
    sid, tid = img._session(db)
    db.query(Tenant).filter_by(id=tid).update({"widget_allow_screen_recording": allowed})
    db.commit()
    return sid, tid


def _folder(tid, sid):
    return f"luran/chat_images/{tid}/{sid}/video"


def _resource(tid, sid, **kw):
    base = {
        "public_id": f"{_folder(tid, sid)}/abc",
        "resource_type": "video",
        "format": "webm",
        "duration": 42.5,
        "bytes": 5_000_000,
        "width": 1280,
        "height": 720,
        "version": 111,
    }
    return {**base, **kw}


def _cm(cm, tid, sid, resource):
    cm.video_folder.return_value = _folder(tid, sid)
    cm.get_video_resource.return_value = resource
    cm.video_delivery_urls.return_value = ("https://c/v.mp4", "https://c/p.jpg")
    cm.destroy.return_value = (True, None)


def test_sign_ok(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        cm.sign_video_upload.return_value = {
            "upload_url": "u",
            "fields": {"a": 1},
            "public_id": "p",
        }
        r = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "upload_url": "u",
        "fields": {"a": 1},
        "max_bytes": 52428800,
        "max_duration_s": 60,
    }


def test_sign_gate_off_403(client, db):
    sid, _ = _seed(db, allowed=False)
    r = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "screen_recording_disabled"


def test_sign_unknown_session_404(client, db):
    r = client.post(
        "/api/v1/chat/video/sign",
        json={"session_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert r.status_code == 404


def test_sign_unconfigured_503(client, db):
    sid, _ = _seed(db)
    with patch(CM) as cm:
        cm.sign_video_upload.return_value = None
        r = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "video_unavailable"


def test_sign_session_video_cap(client, db):
    sid, tid = _seed(db)
    for _ in range(3):
        aid = img._att(db, sid, tid)
        db.query(MessageAttachment).filter_by(id=aid).update(
            {"kind": AttachmentKind.VIDEO, "cloudinary_public_id": f"vid/{aid}"}
        )
    db.commit()
    with patch(CM):
        r = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "video_session_limit"


def test_register_ok_creates_unlinked_video(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, _resource(tid, sid))
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, sid)}/abc"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "video" and body["url"] == "https://c/v.mp4"
    assert body["poster_url"] == "https://c/p.jpg" and body["duration_ms"] == 42500
    row = db.execute(
        select(MessageAttachment).where(MessageAttachment.id == UUID(body["attachment_id"]))
    ).scalar_one()
    assert row.kind == AttachmentKind.VIDEO and row.status == AttachmentStatus.UPLOADED
    assert row.moderation_status == AttachmentModeration.NOT_APPLICABLE
    assert row.content_type == "video/mp4" and row.source_format == "webm"
    assert row.message_id is None


def test_register_foreign_folder_422_without_destroy(client, db):
    sid, tid = _seed(db)
    other_sid, _ = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, None)
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, other_sid)}/abc"},
        )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "video_invalid"
    cm.destroy.assert_not_called()
    cm.get_video_resource.assert_not_called()


def test_register_too_long_destroys(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, _resource(tid, sid, duration=75.0))
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, sid)}/abc"},
        )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "video_too_long"
    cm.destroy.assert_called_once_with(f"{_folder(tid, sid)}/abc", resource_type="video")


def test_register_too_large_and_bad_format(client, db):
    sid, tid = _seed(db)
    pid = f"{_folder(tid, sid)}/abc"
    with patch(CM) as cm:
        _cm(cm, tid, sid, _resource(tid, sid, bytes=60_000_000))
        r1 = client.post(
            "/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid}
        )
        _cm(cm, tid, sid, _resource(tid, sid, format="avi"))
        r2 = client.post(
            "/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid}
        )
    assert r1.json()["detail"]["code"] == "video_too_large"
    assert r2.json()["detail"]["code"] == "video_invalid"


def test_register_twice_409_with_same_id(client, db):
    sid, tid = _seed(db)
    pid = f"{_folder(tid, sid)}/abc"
    with patch(CM) as cm:
        _cm(cm, tid, sid, _resource(tid, sid))
        first = client.post(
            "/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid}
        )
        second = client.post(
            "/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid}
        )
    assert first.status_code == 200 and second.status_code == 409
    assert second.json()["detail"]["attachment_id"] == first.json()["attachment_id"]
    assert db.query(MessageAttachment).filter_by(cloudinary_public_id=pid).count() == 1


def test_register_missing_asset_422(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, None)
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, sid)}/abc"},
        )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "video_invalid"


def test_register_lookup_error_503_without_destroy(client, db):
    sid, tid = _seed(db)
    with patch(CM) as cm:
        _cm(cm, tid, sid, None)
        cm.get_video_resource.side_effect = RuntimeError("cloudinary down")
        r = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": f"{_folder(tid, sid)}/abc"},
        )
    assert r.status_code == 503
    assert r.json()["detail"] == {
        "code": "video_unavailable",
        "message": "Video upload is not available",
    }
    cm.destroy.assert_not_called()


def test_register_concurrent_race_409_with_existing_id(client, db):
    """Another request wins the insert after our existence check: the partial unique
    index raises IntegrityError on commit and we answer 409 with the winner's id.

    The winner row is pre-inserted and the first existence check is stubbed to miss
    (simulating the race window). The re-select after rollback is stubbed too, because
    the test session shares one outer transaction that rollback() would also unwind
    (in production the winner is committed by a separate session and survives).
    """
    sid, tid = _seed(db)
    pid = f"{_folder(tid, sid)}/abc"
    aid = img._att(db, sid, tid)
    db.query(MessageAttachment).filter_by(id=aid).update(
        {"kind": AttachmentKind.VIDEO, "cloudinary_public_id": pid}
    )
    db.commit()
    winner = SimpleNamespace(id=aid)

    with (
        patch(CM) as cm,
        patch("app.api.v1.endpoints.chat_video._existing_video", side_effect=[None, winner]) as ev,
    ):
        _cm(cm, tid, sid, _resource(tid, sid))
        r = client.post(
            "/api/v1/chat/video/register", json={"session_id": str(sid), "public_id": pid}
        )
    assert ev.call_count == 2
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "already_registered"
    assert r.json()["detail"]["attachment_id"] == str(aid)
