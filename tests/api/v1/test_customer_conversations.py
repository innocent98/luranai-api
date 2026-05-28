"""API tests for B2C customer conversations and pagination."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.models.conversation import ConversationSession, Message, MessageRole, SessionStatus
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant
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
