"""API tests for B2C customer conversations and pagination."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities
from app.schemas.conversation import ChatResponse, MessageResponse


def _create_tenant(db: Session) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name="conversation_test_tenant",
        display_name="Conversation Test Tenant",
        public_slug="conversation-test-tenant",
        description="Conversation tenant description",
        widget_logo_url="https://example.com/logo.png",
        widget_primary_color="#111111",
        widget_accent_color="#222222",
        widget_greeting_message="Hello from support",
        is_publicly_listed=True,
        api_key_hash="conversation_hash",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _create_customer(db: Session) -> CustomerUser:
    customer = CustomerUser(
        id=uuid4(),
        full_name="Conversation Customer",
        email="conversation.customer@example.com",
        hashed_password="hashed",
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _customer_headers(customer: CustomerUser) -> dict[str, str]:
    token = create_access_token(
        subject=str(customer.id),
        additional_claims={
            "email": customer.email,
            "auth_type": "customer",
        },
    )
    return {"Authorization": f"Bearer {token}"}


def _create_session(
    db: Session,
    tenant: Tenant,
    customer: CustomerUser,
    *,
    customer_user_id=None,
    last_message_at: datetime | None = None,
) -> ConversationSession:
    session = ConversationSession(
        id=uuid4(),
        tenant_id=tenant.id,
        user_identifier=customer.chat_user_key,
        customer_user_id=customer_user_id,
        status=SessionStatus.ACTIVE,
        last_message_at=last_message_at or datetime.now(UTC),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _add_message(
    db: Session,
    session: ConversationSession,
    *,
    content: str,
    created_at: datetime,
    role: MessageRole = MessageRole.USER,
) -> Message:
    message = Message(
        id=uuid4(),
        session_id=session.id,
        role=role,
        content=content,
        created_at=created_at,
    )
    db.add(message)
    session.last_message_at = created_at
    db.commit()
    db.refresh(message)
    db.refresh(session)
    return message


def test_customer_conversations_list_and_read_flow(client: TestClient, db: Session):
    tenant = _create_tenant(db)
    customer = _create_customer(db)
    customer_id = customer.id
    headers = _customer_headers(customer)

    older_time = datetime.now(UTC) - timedelta(hours=2)
    newer_time = datetime.now(UTC) - timedelta(hours=1)
    legacy_session = _create_session(
        db,
        tenant,
        customer,
        customer_user_id=None,
        last_message_at=older_time,
    )
    current_session = _create_session(
        db,
        tenant,
        customer,
        customer_user_id=customer.id,
        last_message_at=newer_time,
    )
    legacy_session_id = legacy_session.id
    current_session_id = current_session.id

    _add_message(db, legacy_session, content="Legacy hello", created_at=older_time)
    _add_message(db, current_session, content="Current hello", created_at=newer_time)

    list_response = client.get("/api/v1/customer/conversations?limit=1", headers=headers)
    assert list_response.status_code == 200
    data = list_response.json()
    assert data["has_more"] is True
    assert len(data["conversations"]) == 1
    assert data["conversations"][0]["session_id"] == str(current_session_id)
    assert data["conversations"][0]["has_unread"] is True

    cursor = data["conversations"][0]["last_message_at"]
    second_page = client.get(
        f"/api/v1/customer/conversations?limit=1&before={cursor}",
        headers=headers,
    )
    assert second_page.status_code == 200
    second_data = second_page.json()
    assert second_data["conversations"][0]["session_id"] == str(legacy_session_id)

    read_response = client.post(
        f"/api/v1/customer/conversations/{legacy_session_id}/read",
        headers=headers,
    )
    assert read_response.status_code == 200
    refreshed_legacy_session = db.get(ConversationSession, legacy_session_id)
    assert refreshed_legacy_session is not None
    assert refreshed_legacy_session.customer_user_id == customer_id
    assert refreshed_legacy_session.customer_last_read_at is not None


def test_customer_conversation_detail_and_messages_pagination(
    client: TestClient,
    db: Session,
):
    tenant = _create_tenant(db)
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    session = _create_session(db, tenant, customer, customer_user_id=customer.id)

    base = datetime.now(UTC) - timedelta(minutes=5)
    _add_message(db, session, content="First", created_at=base + timedelta(minutes=1))
    _add_message(db, session, content="Second", created_at=base + timedelta(minutes=2))
    _add_message(db, session, content="Third", created_at=base + timedelta(minutes=3))

    detail_response = client.get(
        f"/api/v1/customer/conversations/{session.id}?limit=2",
        headers=headers,
    )
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["business_name"] == tenant.display_name
    assert detail["business_widget_primary_color"] == tenant.widget_primary_color
    assert detail["business_widget_greeting_message"] == tenant.widget_greeting_message
    # Gate flag defaults to the tenant default (True) and is present on the payload.
    assert detail["allow_file_upload"] is True
    assert detail["has_more_messages"] is True
    assert [m["content"] for m in detail["conversation"]["messages"]] == ["Second", "Third"]

    cursor = detail["conversation"]["messages"][0]["created_at"]
    messages_response = client.get(
        f"/api/v1/customer/conversations/{session.id}/messages?limit=2&before={cursor}",
        headers=headers,
    )
    assert messages_response.status_code == 200
    payload = messages_response.json()
    assert payload["has_more"] is False
    assert [m["content"] for m in payload["messages"]] == ["First"]


def test_customer_conversation_invalid_before_returns_422(client: TestClient, db: Session):
    tenant = _create_tenant(db)
    customer = _create_customer(db)
    session = _create_session(db, tenant, customer, customer_user_id=customer.id)
    headers = _customer_headers(customer)

    response = client.get(
        f"/api/v1/customer/conversations/{session.id}/messages?before=not-a-timestamp",
        headers=headers,
    )
    assert response.status_code == 422


def test_customer_send_message_uses_customer_context(client: TestClient, db: Session, monkeypatch):
    from app.api.v1.endpoints import customer_conversations as customer_conversations_endpoint

    tenant = _create_tenant(db)
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    existing_session = _create_session(db, tenant, customer, customer_user_id=customer.id)

    async def fake_send_message(message_request, db):
        assert message_request.user_identifier == customer.chat_user_key
        assert message_request.metadata["customer_email"] == customer.email
        return ChatResponse(
            session_id=existing_session.id,
            message=MessageResponse(
                id=uuid4(),
                session_id=existing_session.id,
                role=MessageRole.ASSISTANT,
                content="Hello customer",
                retrieved_chunk_ids=None,
                token_count=None,
                meta=None,
                created_at=datetime.now(UTC),
            ),
            should_escalate=False,
            escalation_reason=None,
            ticket_id=None,
        )

    monkeypatch.setattr(customer_conversations_endpoint, "send_message", fake_send_message)

    response = client.post(
        "/api/v1/customer/conversations/message",
        headers=headers,
        json={
            "message": "Hi there",
            "session_id": str(existing_session.id),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["message"]["content"] == "Hello customer"


def test_customer_conversation_detail_reflects_allow_file_upload_false(
    client: TestClient, db: Session
):
    """Gate flag is wired to the real tenant column, not hard-coded True."""
    tenant = _create_tenant(db)
    tenant.widget_allow_file_upload = False
    db.commit()
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    session = _create_session(db, tenant, customer, customer_user_id=customer.id)

    detail_response = client.get(
        f"/api/v1/customer/conversations/{session.id}",
        headers=headers,
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["allow_file_upload"] is False


def _patched_chat_pipeline():
    """Stub the RAG + automation layers so send_message runs without OpenAI/Redis."""

    async def _gen(
        self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
    ):
        return "Thanks for the image.", 12, False

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


def test_customer_send_message_forwards_attachment_ids(client: TestClient, db: Session):
    """An authed customer's attachment_ids must thread through to send_message and link.

    Without forwarding, the reconstructed MessageRequest dropped attachment_ids and the
    image was silently never linked to the user message.
    """
    tenant = _create_tenant(db)
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
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    session = _create_session(db, tenant, customer, customer_user_id=customer.id)

    attachment = MessageAttachment(
        session_id=session.id,
        tenant_id=tenant.id,
        direction=AttachmentDirection.INBOUND,
        cloudinary_public_id="chat/customer-x",
        url="https://res.cloudinary.com/x.jpg",
        content_type="image/png",
        width=4,
        height=3,
        bytes=10,
        moderation_status=AttachmentModeration.PASSED,
        status=AttachmentStatus.UPLOADED,
    )
    db.add(attachment)
    db.commit()
    attachment_id = attachment.id

    p1, p2, p3 = _patched_chat_pipeline()
    with p1, p2, p3:
        response = client.post(
            "/api/v1/customer/conversations/message",
            headers=headers,
            json={
                "message": "look at this",
                "session_id": str(session.id),
                "attachment_ids": [str(attachment_id)],
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [a["id"] for a in body["user_message_attachments"]] == [str(attachment_id)]

    row = db.execute(
        select(MessageAttachment).where(MessageAttachment.id == attachment_id)
    ).scalar_one()
    db.refresh(row)
    assert row.status == AttachmentStatus.LINKED
    assert row.message_id is not None
    user_msg = db.execute(select(Message).where(Message.id == row.message_id)).scalar_one()
    assert user_msg.content == "look at this"


def test_customer_conversation_detail_exposes_first_responder(client: TestClient, db: Session):
    """N3 follow-up: the authed detail carries the tenant's real first_responder."""
    tenant = _create_tenant(db)
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    session = _create_session(db, tenant, customer, customer_user_id=customer.id)

    # No AI config row -> schema default.
    r = client.get(f"/api/v1/customer/conversations/{session.id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["first_responder"] == "ai"

    db.add(TenantAIConfig(tenant_id=tenant.id, first_responder="human"))
    db.commit()
    r = client.get(f"/api/v1/customer/conversations/{session.id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["first_responder"] == "human"
