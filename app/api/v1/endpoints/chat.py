"""Chat and conversation endpoints."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from loguru import logger

from app.api.deps import get_current_tenant_admin, get_current_user, get_db, verify_tenant_access, verify_subscription_active
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
    ConversationSessionUpdate,
)
from app.schemas.user import UserResponse
from app.db.models.conversation import SessionStatus, MessageRole, TicketPriority, EscalationAction
from app.schemas.rag import EscalationDecision, ResponseQuality, PromptComponents
from app.utils.http_rate_limiter import chat_rate_limit

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
) -> TicketPriority:
    """
    Determine ticket priority based on query content and escalation reason.

    Args:
        user_query: User's message
        escalation_reason: Why it was escalated

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
        # User explicitly requested human support or critical keyword detected
        return TicketPriority.HIGH

    if "user requested human" in reason_lower:
        # Explicit human request
        return TicketPriority.HIGH

    if "no relevant knowledge" in reason_lower:
        # AI has no knowledge at all
        return TicketPriority.MEDIUM

    # LOW: Default for other escalations (e.g., response quality issues)
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


@router.post(
    "/message",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(chat_rate_limit)]
)
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

        # Check conversation limit before creating new session
        from datetime import datetime, timedelta
        capabilities = tenant_service.get_capabilities(message_data.tenant_id)

        if capabilities and capabilities.max_conversations_per_month is not None:
            # Get start of current month
            from datetime import timezone as tz
            now = datetime.now(tz.utc)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            # Count conversations this month
            from app.db.models.conversation import ConversationSession
            monthly_count = db.query(ConversationSession).filter(
                ConversationSession.tenant_id == message_data.tenant_id,
                ConversationSession.created_at >= month_start,
            ).count()

            # Check limit
            if monthly_count >= capabilities.max_conversations_per_month:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "conversation_limit_reached",
                        "message": f"Monthly conversation limit of {capabilities.max_conversations_per_month} reached.",
                        "current_count": monthly_count,
                        "limit": capabilities.max_conversations_per_month,
                        "resets_at": (month_start + timedelta(days=32)).replace(day=1).isoformat(),
                    }
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

        # Auto-reactivate ABANDONED sessions when user returns
        if session.status == SessionStatus.ABANDONED:
            logger.info(f"User returned to abandoned session {session_id}, reactivating...")
            session = conversation_service.update_session(
                session_id,
                ConversationSessionUpdate(status=SessionStatus.ACTIVE)
            )
            logger.info(f"Session {session_id} reactivated from ABANDONED to ACTIVE")

    # Step 2: Get tenant and AI configuration
    tenant = tenant_service.get_tenant_by_id(session.tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found"
        )

    # Check subscription status (trial expiration, etc.)
    tenant_model = tenant_service.get_tenant(session.tenant_id)
    if tenant_model:
        from datetime import datetime

        # Check trial expiration
        if tenant_model.subscription_status == "trialing":
            from datetime import timezone as tz
            now = datetime.now(tz.utc)
            if tenant_model.trial_ends_at and now > tenant_model.trial_ends_at:
                # Auto-update status
                tenant_model.subscription_status = "trial_expired"
                db.commit()

                raise HTTPException(
                    status_code=402,  # Payment Required
                    detail={
                        "error": "trial_expired",
                        "message": "Your 30-day trial has expired. Please upgrade to continue using Luran.",
                        "trial_ended": tenant_model.trial_ends_at.isoformat(),
                        "upgrade_url": "/dashboard/upgrade",
                    }
                )

        # Check subscription status
        if tenant_model.subscription_status in ["trial_expired", "canceled", "past_due"]:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "subscription_inactive",
                    "message": f"Subscription {tenant_model.subscription_status}. Please update payment.",
                    "status": tenant_model.subscription_status,
                    "upgrade_url": "/dashboard/billing",
                }
            )

    tenant_config = tenant_service.get_ai_config(session.tenant_id)
    if not tenant_config:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant AI configuration not found"
        )

    # Step 3: Check for pending escalation (consent-based escalation)
    session_meta = session.meta or {}
    pending_escalation = session_meta.get('pending_escalation', False)
    escalation_context = session_meta.get('escalation_context', {})

    if pending_escalation:
        # User was asked if they want escalation - check their response
        consent_decision = rag_service.detect_escalation_consent(message_data.message)

        if consent_decision == "consent":
            # User agreed to escalation - escalate now
            logger.info("User consented to escalation")

            # Add user message
            user_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.USER,
                content=message_data.message
            )

            # Clear pending escalation from session
            session_meta['pending_escalation'] = False
            session_meta['escalation_context'] = {}
            conversation_service.update_session_meta(session_id, session_meta)

            # Escalate session
            escalation_reason = escalation_context.get('reason', 'User consented to escalation')
            conversation_service.escalate_session(session_id=session_id, reason=escalation_reason)

            # Create ticket (using existing logic below)
            # Add assistant confirmation message
            confirmation_text = "Perfect! Connecting you with someone from our team now.\n\nSomeone from our support team will be with you shortly."
            assistant_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=confirmation_text,
                meta={'escalation_confirmed': True}
            )

            # Create ticket (copy logic from below)
            ticket_id = None
            try:
                from app.schemas.ticket import TicketCreate
                from app.db.models.conversation import TicketPriority
                from app.db.models.user import UserRole
                from app.utils.email import send_ticket_escalation_email

                ticket_service = TicketService(db)
                conversation_history = conversation_service.get_session_messages(session_id=session_id, limit=20)

                summary = _generate_ticket_summary(
                    conversation_history=conversation_history,
                    escalation_reason=escalation_reason,
                    user_query=escalation_context.get('original_query', message_data.message),
                )

                priority = _determine_ticket_priority(
                    user_query=escalation_context.get('original_query', message_data.message),
                    escalation_reason=escalation_reason,
                )

                ticket_data = TicketCreate(
                    tenant_id=session.tenant_id,
                    session_id=session_id,
                    priority=priority,
                    summary=summary,
                )
                ticket = ticket_service.create_ticket(ticket_data)
                ticket_id = ticket.id
                logger.info(f"Ticket created: {ticket.ticket_number}")

                # Send email notifications
                tenant = tenant_service.get_tenant_by_id(session.tenant_id, include_relations=False)
                from app.db.models.user import User
                notification_recipients = db.query(User).filter(
                    User.tenant_id == session.tenant_id,
                    User.is_active == True,
                    User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT])
                ).all()

                for recipient in notification_recipients:
                    try:
                        email_sent = await send_ticket_escalation_email(
                            email_to=recipient.email,
                            user_name=recipient.full_name,
                            tenant_name=tenant.display_name,
                            ticket_number=ticket.ticket_number,
                            escalation_reason=escalation_reason,
                            customer_query=escalation_context.get('original_query', message_data.message),
                            ticket_priority=priority.value.upper(),
                            ticket_id=str(ticket.id),
                        )
                        if email_sent:
                            logger.info(f"✓ Escalation email sent to {recipient.email}")
                    except Exception as email_error:
                        logger.error(f"✗ Email error: {email_error}")

            except Exception as ticket_error:
                logger.error(f"Failed to create ticket: {ticket_error}")

            return ChatResponse(
                session_id=session_id,
                message=assistant_message,
                should_escalate=True,
                escalation_reason=escalation_reason,
                ticket_id=ticket_id,
            )

        elif consent_decision == "reject":
            # User declined escalation - check if they also asked a new question
            logger.info("User declined escalation offer")

            # Clear pending escalation
            session_meta['pending_escalation'] = False
            session_meta['escalation_context'] = {}
            conversation_service.update_session_meta(session_id, session_meta)

            # Check if message is a simple rejection or has additional content
            message_words = message_data.message.strip().split()
            is_simple_rejection = len(message_words) <= 3  # "no", "nope", "no thanks"

            # If simple rejection, send acknowledgment and return
            if is_simple_rejection:
                # Add user message
                user_message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.USER,
                    content=message_data.message
                )

                # Send acknowledgment
                ack_text = "Sounds good! Let me know if you need anything else."
                assistant_message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=ack_text,
                    meta={'escalation_declined': True}
                )

                return ChatResponse(
                    session_id=session_id,
                    message=assistant_message,
                    should_escalate=False,
                    escalation_reason=None,
                    ticket_id=None,
                )
            else:
                # Message has additional content - treat as new question
                # Continue to normal flow below (don't return here)
                logger.info(f"Rejection contains additional content ({len(message_words)} words), treating as new question")
        else:
            # Unclear response - treat as normal question and try to answer
            logger.info("Unclear consent response, treating as new question")
            # Clear pending escalation and continue to normal flow
            session_meta['pending_escalation'] = False
            session_meta['escalation_context'] = {}
            conversation_service.update_session_meta(session_id, session_meta)

    # Step 4: Add user message to database
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
            max_chunks=3,  # Retrieve 3, re-rank to top 3 for comprehensive answers
            score_threshold=rag_service.MIN_SCORE_THRESHOLD,
        )
        logger.info(f"Retrieved {len(retrieved_chunks)} chunks with avg similarity: {sum(similarity_scores) / len(similarity_scores):.2f}" if similarity_scores else f"Retrieved {len(retrieved_chunks)} chunks")

        # Step 5: Determine initial escalation strategy
        escalation_decision = rag_service.should_escalate(
            user_query=message_data.message,
            ai_config=tenant_config,
            retrieved_chunks_count=len(retrieved_chunks),
            similarity_scores=similarity_scores,
        )
        logger.info(f"Initial escalation action: {escalation_decision.action.value}")

        # Step 6: Get conversation history for context (needed for ticket creation too)
        conversation_history = conversation_service.get_session_messages(
            session_id=session_id,
            limit=20  # Last 10 turns (user + assistant) - Increased from 10 for better context
        )

        # Step 7: Generate AI response based on escalation action
        # Initialize tracking variables
        should_skip_generation = False
        skip_reason = None

        if escalation_decision.action == EscalationAction.AUTO_ESCALATE:
            # User explicitly requested human - acknowledge and escalate
            ai_response_text = "Of course! Connecting you with someone from our team now."
            token_count = 0
            quality = ResponseQuality(is_substantial=True, has_uncertainty=False, word_count=0, char_count=0)
            logger.info("Auto-escalating due to user request")

        elif escalation_decision.action == EscalationAction.OFFER_ESCALATION:
            # No knowledge available - offer escalation immediately
            ai_response_text = "I don't have information about that. Would you like me to connect you with someone from our team?"
            token_count = 0
            quality = ResponseQuality(is_substantial=False, has_uncertainty=True, word_count=0, char_count=0)
            logger.info("No knowledge available, offering escalation")

        else:
            # no_escalation - AI has knowledge, but check similarity first

            # HARD GATE: Skip generation if similarity too low

            if similarity_scores:
                avg_similarity = sum(similarity_scores) / len(similarity_scores)
                if avg_similarity < rag_service.MIN_RELEVANT_SIMILARITY:
                    should_skip_generation = True
                    skip_reason = f"low_similarity_{avg_similarity:.2f}"
                    logger.warning(f"Hard gate triggered: avg_similarity={avg_similarity:.2f} < {rag_service.MIN_RELEVANT_SIMILARITY}")

            if should_skip_generation:
                # Don't call Gemini - return canned response
                ai_response_text = "I don't have information about that. Would you like me to connect you with someone from our team?"
                token_count = 0
                quality = ResponseQuality(
                    is_substantial=False,
                    has_uncertainty=True,
                    word_count=0,
                    char_count=0
                )
                escalation_decision.action = EscalationAction.OFFER_ESCALATION
                escalation_decision.reason = f"Hard gate: {skip_reason}"
                logger.info(f"Generation skipped - {skip_reason}")
            else:
                # Original generation logic continues
                logger.info("Generating AI response...")
                from app.core.config import settings
                prompt_components = rag_service.assemble_prompt(
                    ai_config=tenant_config,
                    user_query=message_data.message,
                    retrieved_chunks=retrieved_chunks,
                    conversation_history=conversation_history,
                    company_name=tenant.display_name,
                    company_description=tenant.description,
                    max_history_turns=10,  # Increased from 5 to 10 for better conversation continuity
                )
                logger.info(
                    f"Assembled prompt with {prompt_components.knowledge_chunks_used} chunks, "
                    f"{prompt_components.history_turns_used} turns"
                )

                ai_response_text, token_count, is_api_error = await rag_service.generate_ai_response(
                    prompt=prompt_components,
                    max_tokens=tenant_config.max_response_tokens,
                    model_name=settings.GEMINI_MODEL,
                )
                logger.info(f"Generated response ({token_count} tokens)")

                # If API error, don't evaluate quality or offer escalation
                if is_api_error:
                    logger.error("API error occurred, returning error message without escalation")
                    quality = ResponseQuality(is_substantial=False, has_uncertainty=True, word_count=0, char_count=0)
                else:
                    # Evaluate response quality - skip if satisfaction detected
                    if not escalation_decision.skip_quality_check:
                        quality = rag_service.evaluate_response_quality(ai_response_text)
                        logger.info(
                            f"Response quality: {quality.word_count} words, "
                            f"substantial={quality.is_substantial}, "
                            f"uncertain={quality.has_uncertainty}"
                        )

                        # If response is weak/uncertain, offer escalation
                        if not quality.is_substantial:
                            logger.info("Response quality insufficient, offering escalation")
                            escalation_decision.action = EscalationAction.OFFER_ESCALATION
                            escalation_decision.reason = "AI response quality below threshold"
                            ai_response_text = f"{ai_response_text}\n\nWould you like me to connect you with someone from our team?"
                    else:
                        # Satisfaction detected - skip quality check
                        logger.info("Skipping quality check (satisfaction detected)")
                        quality = ResponseQuality(is_substantial=True, has_uncertainty=False, word_count=len(ai_response_text.split()), char_count=len(ai_response_text))

        # Step 8: Save assistant message
        assistant_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=ai_response_text,
            retrieved_chunk_ids=[str(chunk.id) for chunk in retrieved_chunks],
            token_count=token_count,
            meta={
                'chunks_used': len(retrieved_chunks),
                'escalation_action': escalation_decision.action.value,
                'response_quality': {
                    'is_substantial': quality.is_substantial,
                    'has_uncertainty': quality.has_uncertainty,
                    'word_count': quality.word_count,
                    'char_count': quality.char_count,
                },
            }
        )
        logger.info(f"Saved assistant message: {assistant_message.id}")

        # Track metrics for monitoring
        if similarity_scores:
            avg_similarity = sum(similarity_scores) / len(similarity_scores)
            rag_service.track_retrieval_metrics(
                query=message_data.message,
                retrieved_chunks_count=len(retrieved_chunks),
                avg_similarity=avg_similarity,
                response_quality=quality,
                escalated=(escalation_decision.action != EscalationAction.NO_ESCALATION),
                generation_skipped=should_skip_generation,
                skip_reason=skip_reason,
                prompt_tokens=token_count,
            )

        # Step 9: Handle escalation based on action
        ticket_id = None
        should_escalate = False

        if escalation_decision.action == EscalationAction.OFFER_ESCALATION:
            # Store pending escalation in session meta (wait for user consent)
            session_meta = session.meta or {}
            session_meta['pending_escalation'] = True
            session_meta['escalation_context'] = {
                'reason': escalation_decision.reason,
                'original_query': message_data.message,
            }
            conversation_service.update_session_meta(session_id, session_meta)
            logger.info("Pending escalation stored, waiting for user consent")

        elif escalation_decision.action == EscalationAction.AUTO_ESCALATE:
            # User explicitly requested human or critical keyword detected - escalate immediately
            should_escalate = True
            logger.info("Auto-escalating: Creating ticket...")
            conversation_service.escalate_session(
                session_id=session_id,
                reason=escalation_decision.reason,
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
                    escalation_reason=escalation_decision.reason,
                    user_query=message_data.message,
                )

                # Determine priority based on keywords and escalation reason
                priority = _determine_ticket_priority(
                    user_query=message_data.message,
                    escalation_reason=escalation_decision.reason,
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
                            escalation_reason=escalation_decision.reason,
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

        # Step 10: Return response
        return ChatResponse(
            session_id=session_id,
            message=assistant_message,
            should_escalate=should_escalate,
            escalation_reason=escalation_decision.reason if should_escalate else None,
            ticket_id=ticket_id,
        )

    except Exception as e:
        logger.error(f"Error processing chat message: {e}")

        # Save error message for user
        error_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content="I apologize, but I'm having trouble processing your request. Please try again or contact support.",
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
