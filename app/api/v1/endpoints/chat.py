"""Chat and conversation endpoints."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from loguru import logger

from app.api.deps import get_current_tenant_admin, get_current_user, get_db, verify_tenant_access
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
    ConversationSessionCreate,
)
from app.schemas.user import UserResponse
from app.db.models.conversation import SessionStatus, MessageRole, TicketPriority

router = APIRouter()


def _generate_ticket_summary(
    conversation_history: list,
    escalation_reason: str,
    user_query: str,
    max_length: int = 200,
) -> str:
    """
    Generate a smart summary for the ticket based on conversation context.

    Args:
        conversation_history: List of previous messages
        escalation_reason: Why the conversation was escalated
        user_query: Current user query that triggered escalation
        max_length: Maximum summary length

    Returns:
        Concise summary of the issue
    """
    # Extract key context from recent messages
    context_parts = []

    # Get last few user messages to understand the issue progression
    user_messages = [
        msg.content for msg in conversation_history[-10:]
        if msg.role == MessageRole.USER
    ]

    if len(user_messages) > 1:
        # Multi-turn conversation - show progression
        first_query = user_messages[0][:80]
        context_parts.append(f"Initial query: {first_query}")

        if user_messages[-1] != user_messages[0]:
            latest_query = user_messages[-1][:80]
            context_parts.append(f"Latest: {latest_query}")
    else:
        # Single message - just use it
        context_parts.append(user_query[:150])

    # Add escalation reason
    context_parts.append(f"Reason: {escalation_reason}")

    # Join and truncate
    summary = " | ".join(context_parts)
    if len(summary) > max_length:
        summary = summary[:max_length - 3] + "..."

    return summary


def _determine_ticket_priority(
    user_query: str,
    escalation_reason: str,
    confidence_score: float,
) -> TicketPriority:
    """
    Determine ticket priority based on query content, reason, and confidence.

    Args:
        user_query: User's message
        escalation_reason: Why it was escalated
        confidence_score: AI confidence score

    Returns:
        Appropriate priority level
    """
    query_lower = user_query.lower()
    reason_lower = escalation_reason.lower()

    # URGENT: Critical keywords or explicit escalation
    urgent_keywords = [
        "urgent", "emergency", "critical", "asap", "immediately",
        "down", "broken", "not working", "can't access", "cant access",
        "losing money", "losing customers", "lawsuit", "legal"
    ]

    if any(keyword in query_lower for keyword in urgent_keywords):
        return TicketPriority.URGENT

    if "escalation keyword" in reason_lower:
        # User explicitly requested human support
        return TicketPriority.HIGH

    # HIGH: Very low confidence (AI has no idea)
    if confidence_score < 0.2:
        return TicketPriority.HIGH

    # MEDIUM: Low confidence
    if confidence_score < 0.5:
        return TicketPriority.MEDIUM

    # LOW: Default for other escalations
    return TicketPriority.LOW


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
    tenant = tenant_service.get_tenant_by_id(session_data.tenant_id, include_relations=False)

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found"
        )

    # Convert SessionCreateRequest to ConversationSessionCreate
    session_create = ConversationSessionCreate(
        tenant_id=session_data.tenant_id,
        user_identifier=session_data.user_identifier,
        meta=session_data.metadata or {},
    )

    session = conversation_service.create_session(session_create)

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

        # Convert to ConversationSessionCreate
        session_create = ConversationSessionCreate(
            tenant_id=message_data.tenant_id,
            user_identifier=message_data.user_identifier,
            meta=message_data.metadata or {},
        )
        session = conversation_service.create_session(session_create)
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
    tenant_config = tenant_service.get_ai_config(session.tenant_id)
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

        # Step 7: Get conversation history for context (needed for ticket creation too)
        conversation_history = conversation_service.get_session_messages(
            session_id=session_id,
            limit=20  # Last 10 turns (user + assistant)
        )

        # Step 8: Check if grounding requirement prevents AI response
        use_system_message = (
            tenant_config.grounding_requirement and
            len(retrieved_chunks) == 0
        )

        if use_system_message:
            # When grounding is required but no knowledge found, use simple system message
            ai_response_text = "I don't have specific information about that. Let me connect you with someone who can help."
            token_count = 0
            logger.info("Using system message due to grounding requirement with no knowledge")
            # Force escalation
            if not should_escalate:
                should_escalate = True
                escalation_reason = "No knowledge available with grounding requirement enabled"
        else:
            # Step 9: Assemble prompt with 3-layer architecture
            logger.info("Assembling prompt...")
            prompt = rag_service.assemble_prompt(
                ai_config=tenant_config,
                user_query=message_data.message,
                retrieved_chunks=retrieved_chunks,
                conversation_history=conversation_history,
                max_history_turns=10,
            )

            # Step 10: Generate AI response
            logger.info("Generating AI response...")
            ai_response_text, token_count = await rag_service.generate_ai_response(
                prompt=prompt,
                max_tokens=tenant_config.max_response_tokens,
            )
            logger.info(f"Generated response ({token_count} tokens)")

        # Step 11: Save assistant message
        assistant_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=ai_response_text,
            retrieved_chunk_ids=[str(chunk.id) for chunk in retrieved_chunks],  # Convert UUID to string for JSON
            confidence_score=confidence_score,
            token_count=token_count,
            meta={
                'chunks_used': len(retrieved_chunks),
                'similarity_scores': similarity_scores,
            }
        )
        logger.info(f"Saved assistant message: {assistant_message.id}")

        # Step 12: Handle escalation if needed
        ticket_id = None
        if should_escalate:
            logger.info("Creating escalation ticket...")
            conversation_service.escalate_session(
                session_id=session_id,
                reason=escalation_reason,
            )

            # Create support ticket
            try:
                from app.schemas.ticket import TicketCreate
                from app.db.models.conversation import TicketPriority
                from app.db.models.user import UserRole
                from app.utils.email import send_ticket_escalation_email

                ticket_service = TicketService(db)

                # Generate smart summary from conversation
                summary = _generate_ticket_summary(
                    conversation_history=conversation_history,
                    escalation_reason=escalation_reason,
                    user_query=message_data.message,
                )

                # Determine priority based on keywords and confidence
                priority = _determine_ticket_priority(
                    user_query=message_data.message,
                    escalation_reason=escalation_reason,
                    confidence_score=confidence_score,
                )

                # Create ticket
                ticket_data = TicketCreate(
                    tenant_id=session.tenant_id,
                    session_id=session_id,
                    priority=priority,
                    summary=summary,
                )
                ticket = ticket_service.create_ticket(ticket_data)
                ticket_id = ticket.id

                logger.info(f"Ticket created: {ticket.ticket_number} with {priority} priority")

                # Send email notifications to tenant admins and support agents
                tenant = tenant_service.get_tenant_by_id(session.tenant_id, include_relations=False)

                # Get all tenant admins and support agents
                from app.db.models.user import User
                notification_recipients = db.query(User).filter(
                    User.tenant_id == session.tenant_id,
                    User.is_active == True,
                    User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT])
                ).all()

                # Send email to each recipient
                for recipient in notification_recipients:
                    try:
                        email_sent = await send_ticket_escalation_email(
                            email_to=recipient.email,
                            user_name=recipient.full_name,
                            tenant_name=tenant.display_name,
                            ticket_number=ticket.ticket_number,
                            escalation_reason=escalation_reason,
                            customer_query=message_data.message,
                            ticket_priority=priority.value.upper(),
                            ticket_id=str(ticket.id),
                        )
                        if email_sent:
                            logger.info(f"✓ Escalation email sent successfully to {recipient.email}")
                        else:
                            logger.error(f"✗ Failed to send escalation email to {recipient.email} (check email configuration and logs above)")
                    except Exception as email_error:
                        # Log but don't fail ticket creation if email fails
                        logger.error(f"✗ Exception sending escalation email to {recipient.email}: {email_error}")

            except Exception as ticket_error:
                logger.error(f"Failed to create ticket or send notifications: {ticket_error}")
                # Don't fail the request if ticket creation fails

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
            meta={'error': str(e)}
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


@router.post(
    "/sessions/{session_id}/takeover",
    response_model=SessionResponse,
    status_code=status.HTTP_200_OK,
)
async def agent_takeover(
    session_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Agent takes over a conversation session.

    This endpoint is called when a support agent wants to handle an escalated
    conversation. It:
    1. Assigns the session to the agent
    2. Assigns the associated ticket to the agent (if exists)
    3. Broadcasts "agent joined" notification via WebSocket

    Args:
        session_id: Session UUID
        db: Database session
        current_user: Current agent user

    Returns:
        Updated session with agent assignment
    """
    from app.db.models.user import UserRole
    from app.core.websocket import connection_manager

    conversation_service = ConversationService(db)

    # Get session to verify tenant access
    session = conversation_service.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Verify tenant access
    verify_tenant_access(session.tenant_id, current_user)

    # Verify user has appropriate role
    if current_user.role not in [UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only support agents or admins can take over conversations",
        )

    # Assign agent to session
    updated_session = conversation_service.assign_agent_to_session(
        session_id=session_id,
        agent_id=current_user.id,
    )

    # If there's an associated ticket, assign it to the agent too
    from app.db.models.conversation import Ticket
    ticket = db.query(Ticket).filter(
        Ticket.session_id == session_id
    ).order_by(Ticket.created_at.desc()).first()

    if ticket:
        from app.services.ticket_service import TicketService
        from app.schemas.ticket import TicketAssign

        ticket_service = TicketService(db)
        try:
            ticket_service.assign_ticket(
                ticket_id=ticket.id,
                assign_data=TicketAssign(assigned_to=current_user.id)
            )
            logger.info(f"Assigned ticket {ticket.ticket_number} to agent {current_user.full_name}")
        except ValueError as e:
            logger.warning(f"Could not assign ticket: {e}")

    # Send system message to indicate agent joined
    conversation_service.add_message(
        session_id=session_id,
        role=MessageRole.SYSTEM,
        content=f"{current_user.full_name} joined the conversation",
        meta={"event": "agent_joined", "agent_id": str(current_user.id)}
    )

    # Broadcast to customer via WebSocket
    await connection_manager.send_to_customer(
        session_id=session_id,
        message={
            "type": "agent_joined",
            "agent_name": current_user.full_name,
            "agent_id": str(current_user.id),
            "timestamp": updated_session.agent_joined_at.isoformat() if updated_session.agent_joined_at else None,
        }
    )

    logger.info(f"Agent {current_user.full_name} took over session {session_id}")

    return updated_session


@router.post(
    "/sessions/{session_id}/agent-message",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_agent_message(
    session_id: UUID,
    message_data: MessageRequest,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Send a message as an agent (alternative to WebSocket for HTTP-based agents).

    This endpoint allows agents to send messages via REST API instead of WebSocket.
    Useful for:
    - Agents who prefer HTTP over WebSocket
    - Automated agent responses
    - Testing and debugging

    The message will be broadcasted to the customer and other agents via WebSocket.

    Args:
        session_id: Session UUID
        message_data: Message content
        db: Database session
        current_user: Current agent user

    Returns:
        Created message
    """
    from app.db.models.user import UserRole
    from app.core.websocket import connection_manager

    conversation_service = ConversationService(db)

    # Get session to verify tenant access
    session = conversation_service.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Verify tenant access
    verify_tenant_access(session.tenant_id, current_user)

    # Verify user has appropriate role
    if current_user.role not in [UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only support agents or admins can send agent messages",
        )

    # Create message
    message = conversation_service.add_message(
        session_id=session_id,
        role=MessageRole.HUMAN_AGENT,
        content=message_data.message,
        meta={
            "agent_id": str(current_user.id),
            "agent_name": current_user.full_name,
        }
    )

    # Broadcast to customer via WebSocket
    await connection_manager.send_to_customer(
        session_id=session_id,
        message={
            "type": "agent_message",
            "message_id": str(message.id),
            "content": message.content,
            "agent_name": current_user.full_name,
            "timestamp": message.created_at.isoformat(),
        }
    )

    # Broadcast to other agents via WebSocket
    await connection_manager.send_to_agents(
        session_id=session_id,
        message={
            "type": "agent_message",
            "message_id": str(message.id),
            "content": message.content,
            "agent_name": current_user.full_name,
            "timestamp": message.created_at.isoformat(),
        },
        exclude_agent_id=current_user.id,
    )

    logger.info(f"Agent {current_user.full_name} sent message in session {session_id}")

    return message
