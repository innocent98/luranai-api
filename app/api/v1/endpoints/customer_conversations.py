"""Authenticated customer conversation endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_customer, get_db
from app.api.v1.endpoints.chat import send_message
from app.db.models.conversation import ConversationSession, Message
from app.db.models.tenant import Tenant
from app.schemas.conversation import ChatResponse, MessageRequest, SessionDetailResponse
from app.schemas.customer_auth import CustomerUserResponse
from app.schemas.customer_conversation import (
    CustomerConversationDetail,
    CustomerConversationSummary,
)
from app.services.conversation_service import ConversationService

router = APIRouter()


def _assert_customer_session_access(
    session: ConversationSession | None,
    current_customer: CustomerUserResponse,
) -> ConversationSession:
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    owner_matches = session.customer_user_id == current_customer.id
    legacy_matches = str(session.user_identifier) == current_customer.chat_user_key

    if not owner_matches and not legacy_matches:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return session


@router.get(
    "/conversations",
    response_model=list[CustomerConversationSummary],
    status_code=status.HTTP_200_OK,
)
def list_customer_conversations(
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    sessions = list(
        db.execute(
            select(ConversationSession)
            .where(
                or_(
                    ConversationSession.customer_user_id == current_customer.id,
                    ConversationSession.user_identifier == current_customer.chat_user_key,
                )
            )
            .order_by(desc(ConversationSession.last_message_at))
        ).scalars()
    )

    summaries: list[CustomerConversationSummary] = []
    for session in sessions:
        if session.customer_user_id is None:
            session.customer_user_id = current_customer.id
        tenant = db.execute(select(Tenant).where(Tenant.id == session.tenant_id)).scalar_one_or_none()
        latest_message = db.execute(
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(desc(Message.created_at))
            .limit(1)
        ).scalar_one_or_none()
        summaries.append(
            CustomerConversationSummary(
                session_id=session.id,
                tenant_id=session.tenant_id,
                business_name=tenant.display_name if tenant else "Business",
                business_slug=tenant.public_slug if tenant else None,
                business_logo_url=tenant.widget_logo_url if tenant else None,
                status=session.status.value,
                last_message_at=session.last_message_at.isoformat(),
                latest_message_preview=latest_message.content if latest_message else None,
            )
        )

    db.commit()
    return summaries


@router.get(
    "/conversations/{session_id}",
    response_model=CustomerConversationDetail,
    status_code=status.HTTP_200_OK,
)
def get_customer_conversation(
    session_id: UUID,
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    session_model = db.execute(
        select(ConversationSession).where(ConversationSession.id == session_id)
    ).scalar_one_or_none()
    session_model = _assert_customer_session_access(session_model, current_customer)

    if session_model.customer_user_id is None:
        session_model.customer_user_id = current_customer.id
        db.commit()
        db.refresh(session_model)

    conversation_service = ConversationService(db)
    detail = conversation_service.get_session(session_id, include_messages=True)
    if not detail or not isinstance(detail, SessionDetailResponse):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    tenant = db.execute(select(Tenant).where(Tenant.id == session_model.tenant_id)).scalar_one_or_none()
    return CustomerConversationDetail(
        business_name=tenant.display_name if tenant else "Business",
        business_slug=tenant.public_slug if tenant else None,
        business_logo_url=tenant.widget_logo_url if tenant else None,
        business_description=tenant.description if tenant else None,
        business_widget_primary_color=tenant.widget_primary_color if tenant else None,
        business_widget_accent_color=tenant.widget_accent_color if tenant else None,
        business_widget_greeting_message=tenant.widget_greeting_message if tenant else None,
        conversation=detail,
    )


@router.post(
    "/conversations/message",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
)
async def send_customer_message(
    payload: MessageRequest,
    db: Session = Depends(get_db),
    current_customer: CustomerUserResponse = Depends(get_current_customer),
):
    conversation_service = ConversationService(db)

    target_session_id = payload.session_id
    if target_session_id:
        session_model = db.execute(
            select(ConversationSession).where(ConversationSession.id == target_session_id)
        ).scalar_one_or_none()
        session_model = _assert_customer_session_access(session_model, current_customer)
        if session_model.customer_user_id is None:
            conversation_service.assign_customer_to_session(session_model.id, current_customer.id)
    else:
        if not payload.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required when starting a new conversation",
            )
        existing_session = conversation_service.get_latest_session_for_customer(
            tenant_id=payload.tenant_id,
            customer_user_id=current_customer.id,
        )
        if existing_session:
            target_session_id = existing_session.id

    chat_response = await send_message(
        MessageRequest(
            message=payload.message,
            session_id=target_session_id,
            tenant_id=payload.tenant_id,
            user_identifier=current_customer.chat_user_key,
            metadata={
                **(payload.metadata or {}),
                "customer_user_id": str(current_customer.id),
                "customer_name": current_customer.full_name,
                "customer_email": current_customer.email,
                "source": "customer_dashboard",
            },
        ),
        db=db,
    )
    conversation_service.assign_customer_to_session(chat_response.session_id, current_customer.id)
    return chat_response
