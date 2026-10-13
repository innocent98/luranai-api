"""Customer write paths require the customer credential, not just a session id.

Staff can see session ids, so a bare `session_id` must never be enough to post as
the customer. Credential = the session's `user_identifier` (what the customer
sockets already check), the session's visitor token, or the authed customer.
"""

import io
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.deps import get_current_customer
from app.core.config import settings
from app.db.models.conversation import ConversationSession, Message, MessageAttachment
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole
from app.db.models.visitor import Visitor, VisitorSource
from app.main import app
from app.schemas.customer_auth import CustomerUserResponse
from app.utils.datetime import utcnow_naive
from tests.api.v1 import test_chat_image_send as img
from tests.rating.conftest import headers, make_user

UPLOAD_CM = "app.api.v1.endpoints.chat.cloudinary_manager"
UPLOAD_MOD = "app.api.v1.endpoints.chat.moderate_image"
VIDEO_CM = "app.api.v1.endpoints.chat_video.cloudinary_manager"


def _chat_session(db):
    """(session_id, tenant_id, user_identifier) with AI config so a send succeeds."""
    sid, tid = img._session(db)
    return sid, tid, db.get(ConversationSession, sid).user_identifier


def _message_count(db, sid) -> int:
    db.expire_all()
    return len(db.execute(select(Message).where(Message.session_id == sid)).scalars().all())


def _send(client, body, **kw):
    p1, p2, p3 = img._patches()
    with p1, p2, p3:
        return client.post("/api/v1/chat/message", json=body, **kw)


# ---- POST /chat/message ----------------------------------------------------------------


def test_bare_session_id_cannot_post_as_customer(client, db):
    sid, _, _ = _chat_session(db)
    r = _send(client, {"session_id": str(sid), "message": "injected"})
    assert r.status_code == 403, r.text
    assert _message_count(db, sid) == 0


def test_staff_with_session_id_cannot_post_as_customer(client, db):
    sid, tid, _ = _chat_session(db)
    admin = make_user(db, db.get(Tenant, tid), role=UserRole.TENANT_ADMIN)
    r = _send(
        client,
        {"session_id": str(sid), "message": "injected by staff"},
        headers=headers(admin),
    )
    assert r.status_code == 403, r.text
    assert _message_count(db, sid) == 0


def test_wrong_user_identifier_rejected(client, db):
    sid, _, _ = _chat_session(db)
    r = _send(client, {"session_id": str(sid), "message": "x", "user_identifier": "nope"})
    assert r.status_code == 403, r.text
    assert _message_count(db, sid) == 0


def test_error_body_is_stable_and_leaks_nothing(client, db):
    sid, _, uid = _chat_session(db)
    r = _send(client, {"session_id": str(sid), "message": "x", "user_identifier": "nope"})
    assert r.json() == {"detail": "Invalid customer credential"}
    assert uid not in r.text


def test_correct_user_identifier_accepted(client, db):
    sid, _, uid = _chat_session(db)
    r = _send(client, {"session_id": str(sid), "message": "hello", "user_identifier": uid})
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] == str(sid)


def test_matching_visitor_token_accepted(client, db):
    sid, tid, _ = _chat_session(db)
    now = utcnow_naive()
    visitor = Visitor(
        tenant_id=tid,
        public_token=f"vtok-{uuid4().hex}",
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WIDGET,
    )
    db.add(visitor)
    db.flush()
    db.get(ConversationSession, sid).visitor_id = visitor.id
    db.commit()
    token = visitor.public_token
    r = _send(
        client,
        {"session_id": str(sid), "message": "hi", "metadata": {"visitor_token": token}},
    )
    assert r.status_code == 200, r.text
    other = _send(
        client,
        {"session_id": str(sid), "message": "hi", "metadata": {"visitor_token": "bogus"}},
    )
    assert other.status_code == 403


def test_unknown_session_still_404(client, db):
    r = _send(client, {"session_id": str(uuid4()), "message": "x", "user_identifier": "u"})
    assert r.status_code == 404


# ---- metadata.customer_user_id --------------------------------------------------------


def _customer(db):
    from app.db.models.customer_user import CustomerUser

    row = CustomerUser(
        full_name="Victim Customer",
        email=f"victim-{uuid4().hex[:8]}@example.com",
        email_hash=uuid4().hex + uuid4().hex,
        hashed_password="x",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_metadata_customer_user_id_cannot_bind_or_reuse_victim_history(client, db):
    sid, tid, _ = _chat_session(db)
    victim = _customer(db)
    victim_session = db.get(ConversationSession, sid)
    victim_session.customer_user_id = victim.id
    db.commit()

    r = _send(
        client,
        {
            "tenant_id": str(tid),
            "user_identifier": f"attacker-{uuid4().hex[:8]}",
            "message": "hello",
            "metadata": {"customer_user_id": str(victim.id), "entry_point": "widget"},
        },
    )
    assert r.status_code == 200, r.text
    new_sid = r.json()["session_id"]
    assert new_sid != str(sid), "must not reuse the victim's session"
    created = db.get(ConversationSession, UUID(new_sid))
    db.refresh(created)
    assert created.customer_user_id is None
    assert "customer_user_id" not in (created.meta or {})


def test_create_session_ignores_metadata_customer_user_id(client, db):
    _, tid, _ = _chat_session(db)
    victim = _customer(db)
    r = client.post(
        "/api/v1/chat/sessions",
        json={
            "tenant_id": str(tid),
            "user_identifier": f"attacker-{uuid4().hex[:8]}",
            "metadata": {"customer_user_id": str(victim.id)},
        },
    )
    assert r.status_code == 201, r.text
    row = db.get(ConversationSession, UUID(r.json()["id"]))
    assert row.customer_user_id is None


def test_authed_customer_endpoint_still_binds_customer(client, db):
    sid, tid, _ = _chat_session(db)
    customer = _customer(db)
    customer_id = customer.id
    now = datetime.now(UTC)
    me = CustomerUserResponse(
        id=customer_id,
        full_name="Victim Customer",
        email="victim@example.com",
        chat_user_key=f"ck-{uuid4().hex[:8]}",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_customer] = lambda: me
    try:
        p1, p2, p3 = img._patches()
        with p1, p2, p3:
            r = client.post(
                "/api/v1/customer/conversations/message",
                json={"tenant_id": str(tid), "message": "hello"},
            )
    finally:
        app.dependency_overrides.pop(get_current_customer, None)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(ConversationSession, UUID(r.json()["session_id"])).customer_user_id == customer_id


# ---- media paths: upload + video ------------------------------------------------------


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 3), "red").save(buf, format="PNG")
    return buf.getvalue()


def _upload(client, sid, **form):
    with (
        patch(UPLOAD_CM) as cm,
        patch(UPLOAD_MOD, new=AsyncMock(return_value=(True, None))),
    ):
        cm.validate_image.return_value = (True, None)
        cm.upload_chat_image.return_value = ("https://c/x.jpg", "chat/x", None)
        return client.post(
            "/api/v1/chat/upload",
            data={"session_id": str(sid), **form},
            files={"file": ("a.png", _png(), "image/png")},
        )


def _allow_uploads(db, tid, video=True):
    db.query(Tenant).filter_by(id=tid).update(
        {"widget_allow_file_upload": True, "widget_allow_screen_recording": video}
    )
    db.commit()


def test_upload_rejects_wrong_user_identifier(client, db):
    sid, tid, _ = _chat_session(db)
    _allow_uploads(db, tid)
    r = _upload(client, sid, user_identifier="nope")
    assert r.status_code == 403, r.text
    assert db.execute(select(MessageAttachment)).scalars().all() == []


def test_upload_accepts_correct_user_identifier(client, db):
    sid, tid, uid = _chat_session(db)
    _allow_uploads(db, tid)
    assert _upload(client, sid, user_identifier=uid).status_code == 200


def test_upload_without_credential_still_works_until_clients_send_one(client, db):
    """Live luran-chat sends only session_id on /chat/upload; enforcement is flag-gated."""
    sid, tid, _ = _chat_session(db)
    _allow_uploads(db, tid)
    assert settings.CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL is False
    assert _upload(client, sid).status_code == 200


def test_upload_requires_credential_when_enforced(client, db, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL", True)
    sid, tid, uid = _chat_session(db)
    _allow_uploads(db, tid)
    assert _upload(client, sid).status_code == 403
    assert _upload(client, sid, user_identifier=uid).status_code == 200


def test_video_sign_and_register_credential(client, db, monkeypatch):
    sid, tid, uid = _chat_session(db)
    _allow_uploads(db, tid)
    signed = {"upload_url": "u", "fields": {}, "public_id": "p"}
    with patch(VIDEO_CM) as cm:
        cm.sign_video_upload.return_value = signed
        bad = client.post(
            "/api/v1/chat/video/sign", json={"session_id": str(sid), "user_identifier": "nope"}
        )
        assert bad.status_code == 403, bad.text
        legacy = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
        assert legacy.status_code == 200, legacy.text
        good = client.post(
            "/api/v1/chat/video/sign", json={"session_id": str(sid), "user_identifier": uid}
        )
        assert good.status_code == 200, good.text
        reg_bad = client.post(
            "/api/v1/chat/video/register",
            json={"session_id": str(sid), "public_id": "x", "user_identifier": "nope"},
        )
        assert reg_bad.status_code == 403, reg_bad.text
        monkeypatch.setattr(settings, "CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL", True)
        enforced = client.post("/api/v1/chat/video/sign", json={"session_id": str(sid)})
        assert enforced.status_code == 403


@pytest.mark.parametrize("path", ["/api/v1/chat/upload", "/api/v1/chat/video/sign"])
def test_media_paths_never_echo_the_credential(client, db, path):
    sid, tid, uid = _chat_session(db)
    _allow_uploads(db, tid)
    if path.endswith("upload"):
        r = _upload(client, sid, user_identifier="nope")
    else:
        r = client.post(path, json={"session_id": str(sid), "user_identifier": "nope"})
    assert r.status_code == 403
    assert uid not in r.text


def _customer_bearer(customer_id) -> dict:
    from datetime import timedelta

    from jose import jwt

    token = jwt.encode(
        {
            "sub": str(customer_id),
            "auth_type": "customer",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


def test_enforced_media_accepts_owning_customer_token_and_rejects_staff(client, db, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL", True)
    sid, tid, _ = _chat_session(db)
    _allow_uploads(db, tid)
    customer = _customer(db)
    customer_id = customer.id
    db.get(ConversationSession, sid).customer_user_id = customer_id
    staff_headers = headers(make_user(db, db.get(Tenant, tid), role=UserRole.TENANT_ADMIN))
    stranger_headers = _customer_bearer(_customer(db).id)
    db.commit()

    def post(hdrs):
        with (
            patch(UPLOAD_CM) as cm,
            patch(UPLOAD_MOD, new=AsyncMock(return_value=(True, None))),
        ):
            cm.validate_image.return_value = (True, None)
            cm.upload_chat_image.return_value = ("https://c/x.jpg", "chat/x", None)
            return client.post(
                "/api/v1/chat/upload",
                data={"session_id": str(sid)},
                files={"file": ("a.png", _png(), "image/png")},
                headers=hdrs,
            )

    assert post(_customer_bearer(customer_id)).status_code == 200
    assert post(staff_headers).status_code == 403
    assert post(stranger_headers).status_code == 403
