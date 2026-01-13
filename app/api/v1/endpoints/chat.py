"""Chat and conversation endpoints."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from loguru import logger

from app.api.deps import get_current_tenant_admin, get_db, verify_tenant_access
from app.services.conversation_service import ConversationService
from app.services.rag_service import RAGService
from app.services.tenant_service import TenantService
from app.services.ticket_service import TicketService
from app.schemas.conversation import (
    SessionResponse,
    SessionDetailResponse,
    MessageResponse,
    MessageRequest,
    ChatResponse,
    SessionCreateRequest,
)
from app.schemas.user import UserResponse
from app.db.models.conversation import SessionStatus, MessageRole

router = APIRouter()


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    session_data: SessionCreateRequest,
    db: Session = Depends(get_db),
):
    """
    Create a new conversation session.

    Args:
        session_data: Session creation data
        db: Database session

    Returns:
        Created session
    """
    conversation_service = ConversationService(db)

    # Verify tenant exists
    tenant_service = TenantService(db)
    tenant = tenant_service.get_tenant(session_data.tenant_id)

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found"
        )

    session = conversation_service.create_session(
        tenant_id=session_data.tenant_id,
        user_identifier=session_data.user_identifier,
        metadata=session_data.metadata,
    )

    return session


@router.post("/message", response_model=ChatResponse, status_code=status.HTTP_200_OK)
async def send_message(
    message_data: MessageRequest,
    db: Session = Depends(get_db),
):
    """
    Send a message to the AI assistant and get a response.

    This endpoint orchestrates the full RAG (Retrieval-Augmented Generation) flow:
    1. Session management (get or create)
    2. Knowledge retrieval via vector search
    3. Confidence calculation
    4. Escalation detection
    5. Prompt assembly with 3-layer architecture
    6. AI response generation via Gemini
    7. Ticket creation if escalated

    Args:
        message_data: User message and session info
        db: Database session

    Returns:
        AI response with metadata and escalation status
    """
    conversation_service = ConversationService(db)
    rag_service = RAGService(db)
    tenant_service = TenantService(db)

    logger.info(f"Processing chat message: session_id={message_data.session_id}")

    # Step 1: Get or create session
    session_id = message_data.session_id
    if not session_id:
        # Create new session if not provided
        if not message_data.tenant_id or not message_data.user_identifier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id and user_identifier are required for new sessions"
            )

        session = conversation_service.create_session(
            tenant_id=message_data.tenant_id,
            user_identifier=message_data.user_identifier,
            metadata=message_data.metadata,
        )
        session_id = session.id
        logger.info(f"Created new session: {session_id}")
    else:
        # Verify session exists
        session = conversation_service.get_session(session_id)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )

    # Step 2: Get tenant AI configuration
    tenant_config = tenant_service.get_tenant_ai_config(session.tenant_id)
    if not tenant_config:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant AI configuration not found"
        )

    # Step 3: Add user message to database
    user_message = conversation_service.add_message(
        session_id=session_id,
        role=MessageRole.USER,
        content=message_data.message
    )
    logger.info(f"Added user message: {user_message.id}")

    try:
        # Step 4: Retrieve relevant knowledge chunks via vector search
        logger.info("Retrieving relevant knowledge chunks...")
        retrieved_chunks, similarity_scores = await rag_service.retrieve_relevant_chunks(
            tenant_id=session.tenant_id,
            query=message_data.message,
            max_chunks=5,
            score_threshold=0.3,
        )
        logger.info(f"Retrieved {len(retrieved_chunks)} chunks with scores: {similarity_scores}")

        # Step 5: Calculate confidence score
        confidence_score = rag_service.calculate_confidence(
            retrieved_chunks=retrieved_chunks,
            similarity_scores=similarity_scores,
        )
        logger.info(f"Confidence score: {confidence_score:.2f}")

        # Step 6: Check if escalation is needed
        should_escalate, escalation_reason = rag_service.should_escalate(
            user_query=message_data.message,
            confidence_score=confidence_score,
            ai_config=tenant_config,
        )

        if should_escalate:
            logger.warning(f"Escalation triggered: {escalation_reason}")

        # Step 7: Get conversation history for context
        conversation_history = conversation_service.get_session_messages(
            session_id=session_id,
            limit=20  # Last 10 turns (user + assistant)
        )

        # Step 8: Assemble prompt with 3-layer architecture
        logger.info("Assembling prompt...")
        prompt = rag_service.assemble_prompt(
            ai_config=tenant_config,
            user_query=message_data.message,
            retrieved_chunks=retrieved_chunks,
            conversation_history=conversation_history,
            max_history_turns=10,
        )

        # Step 9: Generate AI response
        logger.info("Generating AI response...")
        ai_response_text, token_count = await rag_service.generate_ai_response(
            prompt=prompt,
            max_tokens=tenant_config.max_response_tokens,
        )
        logger.info(f"Generated response ({token_count} tokens)")

        # Step 10: Save assistant message
        assistant_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=ai_response_text,
            retrieved_chunk_ids=[chunk.id for chunk in retrieved_chunks],
            confidence_score=confidence_score,
            token_count=token_count,
            metadata={
                'chunks_used': len(retrieved_chunks),
                'similarity_scores': similarity_scores,
            }
        )
        logger.info(f"Saved assistant message: {assistant_message.id}")

        # Step 11: Handle escalation if needed
        ticket_id = None
        if should_escalate:
            logger.info("Creating escalation ticket...")
            conversation_service.escalate_session(
                session_id=session_id,
                escalation_reason=escalation_reason,
            )

            # Create support ticket
            ticket_service = TicketService(db)
            from app.schemas.ticket import TicketCreate
            from app.db.models.ticket import TicketPriority

            ticket_data = TicketCreate(
                tenant_id=session.tenant_id,
                session_id=session_id,
                priority=TicketPriority.MEDIUM,
                summary=f"AI escalation: {escalation_reason}",
            )
            ticket = ticket_service.create_ticket(ticket_data)
            ticket_id = ticket.id
            logger.info(f"Created ticket: {ticket.ticket_number}")

        # Step 12: Return response
        return ChatResponse(
            session_id=session_id,
            message=assistant_message,
            should_escalate=should_escalate,
            escalation_reason=escalation_reason,
            ticket_id=ticket_id,
        )

    except Exception as e:
        logger.error(f"Error processing chat message: {e}")

        # Save error message for user
        error_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content="I apologize, but I'm having trouble processing your request. Please try again or contact support.",
            confidence_score=0.0,
            metadata={'error': str(e)}
        )

        return ChatResponse(
            session_id=session_id,
            message=error_message,
            should_escalate=True,
            escalation_reason="System error during processing",
        )


@router.get(
    "/sessions/{session_id}",
    response_model=SessionDetailResponse,
    status_code=status.HTTP_200_OK,
)
def get_session(
    session_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get conversation session with messages.

    Args:
        session_id: Session UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Session with all messages
    """
    conversation_service = ConversationService(db)
    session = conversation_service.get_session(session_id, include_messages=True)

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Verify tenant access
    verify_tenant_access(session.tenant_id, current_user)

    return session


@router.get(
    "/tenants/{tenant_id}/sessions",
    response_model=list[SessionResponse],
    status_code=status.HTTP_200_OK,
)
def list_tenant_sessions(
    tenant_id: UUID,
    status_filter: Optional[SessionStatus] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500, description="Maximum sessions to return"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    List conversation sessions for a tenant.

    Args:
        tenant_id: Tenant UUID
        status_filter: Optional status filter
        limit: Maximum sessions to return
        db: Database session
        current_user: Current admin user

    Returns:
        List of conversation sessions
    """
    # Verify access
    verify_tenant_access(tenant_id, current_user)

    conversation_service = ConversationService(db)
    sessions = conversation_service.get_tenant_sessions(
        tenant_id, status=status_filter, limit=limit
    )

    return sessions


@router.get(
    "/sessions/{session_id}/messages",
    response_model=list[MessageResponse],
    status_code=status.HTTP_200_OK,
)
def get_session_messages(
    session_id: UUID,
    limit: Optional[int] = Query(None, ge=1, le=500, description="Limit number of messages"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get messages for a conversation session.

    Args:
        session_id: Session UUID
        limit: Optional limit on number of messages
        db: Database session
        current_user: Current admin user

    Returns:
        List of messages ordered by creation time
    """
    conversation_service = ConversationService(db)

    # Get session to verify tenant access
    session = conversation_service.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Verify tenant access
    verify_tenant_access(session.tenant_id, current_user)

    messages = conversation_service.get_session_messages(session_id, limit=limit)
    return messages
