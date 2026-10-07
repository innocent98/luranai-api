"""POST /api/v1/chat/message with image attachments."""

from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    SessionStatus,
)
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities


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
    db.add(
        TenantAIConfig(
            id=uuid4(),
            tenant_id=tenant.id,
            brand_voice={},
            escalation_keywords=[],
            max_response_tokens=250,
            grounding_requirement=True,
        )
    )
    db.add(
        TenantCapabilities(
            id=uuid4(),
            tenant_id=tenant.id,
            can_create_tickets=True,
            can_access_analytics=True,
            can_upload_knowledge=True,
            max_conversations_per_month=1000,
        )
    )
    s = ConversationSession(
        tenant_id=tenant.id, user_identifier=f"v-{prefix}", status=SessionStatus.ACTIVE
    )
    db.add(s)
    db.commit()
    return s.id, tenant.id


def _att(db, session_id, tenant_id, **kw):
    a = MessageAttachment(
        session_id=session_id,
        tenant_id=tenant_id,
        direction=AttachmentDirection.INBOUND,
        cloudinary_public_id="chat/x",
        url="https://c/x.jpg",
        content_type="image/png",
        width=4,
        height=3,
        bytes=10,
        moderation_status=AttachmentModeration.PASSED,
        status=AttachmentStatus.UPLOADED,
        **kw,
    )
    db.add(a)
    db.commit()
    return a.id


_SEEN_IMAGE_URLS: list = []


def _patches():
    async def _gen(
        self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
    ):
        _SEEN_IMAGE_URLS.append(image_urls)
        return "Nice picture.", 30, False

    async def _retr(self, *, tenant_id, query, max_chunks, score_threshold):
        return [], []

    from app.services.chat_automation_orchestrator import AutomationTurnResult

    async def _auto(self, **kwargs):
        return AutomationTurnResult(handled=False)

    return (
        patch("app.services.rag_service.RAGService.generate_ai_response", new=_gen),
        patch("app.services.rag_service.RAGService.retrieve_relevant_chunks", new=_retr),
        patch(
            "app.services.chat_automation_orchestrator.ChatAutomationOrchestrator.handle_turn",
            new=_auto,
        ),
    )


def _send(client, session_id, message, ids):
    _SEEN_IMAGE_URLS.clear()
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        return client.post(
            "/api/v1/chat/message",
            json={"session_id": str(session_id), "message": message, "attachment_ids": ids},
        )


def test_image_only_message_accepted(client, db):
    sid, tid = _session(db)
    aid = _att(db, sid, tid)
    r = _send(client, sid, "", [str(aid)])
    assert r.status_code == 200, r.text
    atts = r.json()["user_message_attachments"]
    assert [a["id"] for a in atts] == [str(aid)]
    assert atts[0]["url"] == "https://c/x.jpg"
    assert atts[0]["content_type"] == "image/png"
    row = db.execute(select(MessageAttachment).where(MessageAttachment.id == aid)).scalar_one()
    user_msg = db.execute(select(Message).where(Message.id == row.message_id)).scalar_one()
    assert user_msg.content == "[image]"


def test_text_and_image_links_and_surfaces(client, db):
    sid, tid = _session(db)
    aid = _att(db, sid, tid)
    r = _send(client, sid, "look at this", [str(aid)])
    assert r.status_code == 200, r.text
    row = db.execute(select(MessageAttachment).where(MessageAttachment.id == aid)).scalar_one()
    db.refresh(row)
    assert row.status == AttachmentStatus.LINKED
    assert row.message_id is not None
    user_msg = db.execute(select(Message).where(Message.id == row.message_id)).scalar_one()
    assert user_msg.content == "look at this"
    assert [a["id"] for a in r.json()["user_message_attachments"]] == [str(aid)]
    assert _SEEN_IMAGE_URLS == [["https://c/x.jpg"]]


def test_foreign_attachment_rejected(client, db):
    sid, tid = _session(db)
    osid, otid = _session(db)
    foreign = _att(db, osid, otid)
    r = _send(client, sid, "hi", [str(foreign)])
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid or already-used attachment"


def test_already_linked_rejected(client, db):
    sid, tid = _session(db)
    aid = _att(db, sid, tid)
    assert _send(client, sid, "one", [str(aid)]).status_code == 200
    r = _send(client, sid, "two", [str(aid)])
    assert r.status_code == 400


def test_unknown_attachment_rejected(client, db):
    sid, _ = _session(db)
    r = _send(client, sid, "hi", [str(uuid4())])
    assert r.status_code == 400


def test_empty_message_without_attachment_422(client, db):
    sid, _ = _session(db)
    r = _send(client, sid, "", [])
    assert r.status_code == 422


def test_guard_links_to_latest_user_message_when_branch_skipped_link(client, db):
    """If no branch linked attachments, the post-turn guard links them (not silently lost)."""
    from app.api.v1.endpoints import chat as chat_module
    from app.db.models.conversation import MessageRole
    from app.schemas.conversation import ChatResponse, MessageResponse
    from app.services.conversation_service import ConversationService

    sid, tid = _session(db)
    aid = _att(db, sid, tid)

    async def _fake_impl(message_data, db_, linked):
        svc = ConversationService(db_)
        svc.add_message(sid, MessageRole.USER, "hello")  # unwrapped: never links
        reply = svc.add_message(sid, MessageRole.ASSISTANT, "hi")
        return ChatResponse(session_id=sid, message=MessageResponse.model_validate(reply))

    with patch.object(chat_module, "_send_message_impl", new=_fake_impl):
        r = client.post(
            "/api/v1/chat/message",
            json={"session_id": str(sid), "message": "hello", "attachment_ids": [str(aid)]},
        )
    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()["user_message_attachments"]] == [str(aid)]
    row = db.execute(select(MessageAttachment).where(MessageAttachment.id == aid)).scalar_one()
    db.refresh(row)
    assert row.status == AttachmentStatus.LINKED
    assert row.message_id is not None


def test_more_than_three_attachments_422(client, db):
    sid, tid = _session(db)
    ids = [str(_att(db, sid, tid)) for _ in range(4)]
    r = _send(client, sid, "hi", ids)
    assert r.status_code == 422
    r3 = _send(client, sid, "hi", ids[:3])
    assert r3.status_code == 200, r3.text
