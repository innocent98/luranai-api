"""Chat and conversation endpoints."""

import re
from datetime import timedelta
from typing import Any, cast
from types import SimpleNamespace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_tenant_admin,
    get_current_user,
    get_db,
    verify_subscription_active,
    verify_tenant_access,
)
from app.core.config import settings
from app.core.websocket import connection_manager
from app.db.models.conversation import (
    ConversationSession,
    EscalationAction,
    MessageRole,
    SessionStatus,
    Ticket,
    TicketPriority,
)
from app.db.models.business_profile import BusinessProfile
from app.db.models.flow import Flow, FlowExecution, FlowStatus
from app.db.models.tenant import TenantAIConfig
from app.db.models.user import User, UserRole
from app.db.models.visitor import VisitorSource
from app.schemas.conversation import (
    ChatResponse,
    ConversationSessionCreate,
    ConversationSessionUpdate,
    MessageRequest,
    MessageResponse,
    SessionCreateRequest,
    SessionDetailResponse,
    SessionResponse,
)
from app.schemas.rag import EscalationDecision, ResponseQuality
from app.schemas.ticket import TicketAssign, TicketCreate
from app.schemas.tenant import TenantResponse
from app.schemas.user import UserResponse
from app.schemas.visitor import VisitorEnrichmentUpdate
from app.db.models.lead import Lead
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.sales.intent import is_trigger_intent
from app.services.conversation_service import ConversationService
from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator
from app.services.flow_engine import FlowEngine
from app.sales.pricing import compute_quote
from app.services.lead_service import LeadService
from app.services.quote_service import QuoteService
from app.services.rag_service import RAGService
from app.services.tenant_service import TenantService
from app.services.ticket_service import TicketService
from app.services.visitor_service import VisitorService
from app.utils.datetime import ensure_utc_aware, utcnow_aware, utcnow_naive
from app.utils.email import send_ticket_escalation_email
from app.utils.http_rate_limiter import chat_rate_limit

router = APIRouter()

SYSTEM_START_TRIGGER = "start"
FLOW_START_PREFIX = "__flow_start__:"
TALK_TO_HUMAN_TOKEN = "__talk_to_human__"
RAG_CHAT_TOKEN = "__rag_chat__"
CONTACT_EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _extract_customer_name(candidate: str | None) -> str | None:
    """Normalize a lightweight customer name from free text."""
    if not candidate:
        return None

    normalized = re.sub(r"\s+", " ", candidate).strip()
    if len(normalized) < 2 or len(normalized) > 100:
        return None
    if CONTACT_EMAIL_PATTERN.fullmatch(normalized.lower()):
        return None

    return normalized


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
        msg.content for msg in conversation_history[-10:] if msg.role == MessageRole.USER
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
        summary = summary[: max_length - 3] + "..."

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
        "urgent",
        "emergency",
        "critical",
        "asap",
        "immediately",
        "down",
        "broken",
        "not working",
        "can't access",
        "cant access",
        "losing money",
        "losing customers",
        "lawsuit",
        "legal",
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


def _has_usable_followup_context(conversation_history: list[MessageResponse]) -> bool:
    """
    Return True when session history contains enough recent exchange context to
    answer a follow-up even if current retrieval returns no chunks.
    """
    if not conversation_history:
        return False

    user_count = 0
    assistant_count = 0
    last_user_content = ""
    last_assistant_content = ""

    for msg in conversation_history:
        content = (msg.content or "").strip()
        if not content:
            continue
        if msg.role == MessageRole.USER:
            user_count += 1
            last_user_content = content
        elif msg.role in {MessageRole.ASSISTANT, MessageRole.HUMAN_AGENT}:
            assistant_count += 1
            last_assistant_content = content

    return (
        user_count >= 1
        and assistant_count >= 1
        and bool(last_user_content)
        and bool(last_assistant_content)
    )


def _extract_valid_email(candidate: str | None) -> str | None:
    """Extract and validate a single email from candidate text."""
    if not candidate:
        return None
    normalized = candidate.strip().lower()
    if not normalized:
        return None
    if CONTACT_EMAIL_PATTERN.fullmatch(normalized):
        return normalized
    return None


def _resolve_customer_contact_email(
    session_meta: dict[str, Any],
    message_metadata: dict[str, Any] | None,
    existing_email: str | None = None,
) -> str | None:
    """Resolve customer contact email from session metadata or request metadata."""
    valid_existing_email = _extract_valid_email(existing_email)
    if valid_existing_email:
        return valid_existing_email

    from_session = session_meta.get("contact_email")
    valid_from_session = _extract_valid_email(
        from_session if isinstance(from_session, str) else None
    )
    if valid_from_session:
        return valid_from_session

    if not isinstance(message_metadata, dict):
        return None

    for key in ("contact_email", "customer_email", "email"):
        raw = message_metadata.get(key)
        if isinstance(raw, str):
            valid = _extract_valid_email(raw)
            if valid:
                return valid

    return None


def _resolve_customer_name(
    session_meta: dict[str, Any],
    message_metadata: dict[str, Any] | None,
    existing_name: str | None = None,
) -> str | None:
    """Resolve customer name from visitor profile, session metadata, or request metadata."""
    valid_existing_name = _extract_customer_name(existing_name)
    if valid_existing_name:
        return valid_existing_name

    from_session = session_meta.get("contact_name")
    valid_from_session = _extract_customer_name(
        from_session if isinstance(from_session, str) else None
    )
    if valid_from_session:
        return valid_from_session

    if not isinstance(message_metadata, dict):
        return None

    for key in ("contact_name", "customer_name", "name"):
        raw = message_metadata.get(key)
        if isinstance(raw, str):
            valid = _extract_customer_name(raw)
            if valid:
                return valid

    return None


def _get_missing_contact_fields(
    *,
    require_name_before_escalation: bool,
    require_email_before_escalation: bool,
    customer_name: str | None,
    customer_contact_email: str | None,
) -> list[str]:
    """Return the ordered list of customer identity fields still needed for escalation."""
    missing_fields: list[str] = []
    if require_name_before_escalation and not customer_name:
        missing_fields.append("name")
    if require_email_before_escalation and not customer_contact_email:
        missing_fields.append("email")
    return missing_fields


def _build_contact_capture_prompt(required_fields: list[str]) -> str:
    """Generate the next customer identity prompt for escalation."""
    next_field = required_fields[0] if required_fields else "email"
    if next_field == "name":
        return "Before I connect you with a support agent, what name should I share with the team?"
    return (
        "Before I connect you with a support agent, please share the best email "
        "address for follow-up."
    )


def _build_contact_capture_retry_prompt(field_name: str) -> str:
    """Generate a validation retry prompt for the requested identity field."""
    if field_name == "name":
        return "Please share your name so I can pass it along to the support team."
    return (
        "Please share a valid email address (for example, name@example.com) so we "
        "can connect you with support and follow up."
    )


def _store_pending_contact_capture(
    conversation_service: ConversationService,
    session_id: UUID,
    session_meta: dict[str, Any],
    escalation_reason: str,
    customer_query: str,
    required_fields: list[str] | None = None,
) -> None:
    """Persist pending identity-capture state for escalation workflow."""
    session_meta["pending_contact_capture"] = True
    session_meta["contact_capture_context"] = {
        "reason": escalation_reason,
        "customer_query": customer_query,
        "required_fields": required_fields or ["email"],
    }
    conversation_service.update_session_meta(session_id, session_meta)


def _resolve_bootstrapped_visitor_context(
    visitor_service: VisitorService,
    tenant_id: UUID,
    metadata: dict[str, Any] | None,
) -> tuple[UUID | None, UUID | None]:
    """Resolve visitor identifiers only when the caller already has bootstrap tokens."""
    if not isinstance(metadata, dict):
        return None, None

    visitor_token = metadata.get("visitor_token")
    visitor_session_token = metadata.get("visitor_session_token")
    entry_point = metadata.get("entry_point")
    if not isinstance(visitor_token, str) or not visitor_token.strip():
        return None, None
    if entry_point not in {VisitorSource.WIDGET.value, VisitorSource.SUPPORT_PAGE.value}:
        return None, None

    visitor = visitor_service.get_visitor_by_token(tenant_id, visitor_token.strip())
    if not visitor:
        return None, None

    visitor_session_id: UUID | None = None
    if isinstance(visitor_session_token, str) and visitor_session_token.strip():
        visitor_session = visitor_service.get_visitor_session_by_token(
            visitor.id, visitor_session_token.strip()
        )
        if visitor_session:
            visitor_session_id = visitor_session.id

    return visitor.id, visitor_session_id


def _resolve_customer_user_id(metadata: dict[str, Any] | None) -> UUID | None:
    """Resolve authenticated customer ID from trusted wrapper metadata."""
    if not isinstance(metadata, dict):
        return None

    raw_customer_user_id = metadata.get("customer_user_id")
    if not isinstance(raw_customer_user_id, str) or not raw_customer_user_id.strip():
        return None

    try:
        return UUID(raw_customer_user_id)
    except (ValueError, TypeError):
        return None


def _persist_contact_email_to_visitor(
    visitor_service: VisitorService,
    session_id: UUID,
    email: str | None,
) -> None:
    """Sync captured contact email into the linked visitor record when present."""
    valid_email = _extract_valid_email(email)
    if not valid_email:
        return
    visitor_service.update_visitor_from_session(
        session_id,
        VisitorEnrichmentUpdate(email=valid_email),
    )


def _persist_contact_name_to_visitor(
    visitor_service: VisitorService,
    session_id: UUID,
    name: str | None,
) -> None:
    """Sync captured customer name into the linked visitor record when present."""
    valid_name = _extract_customer_name(name)
    if not valid_name:
        return
    visitor_service.update_visitor_from_session(
        session_id,
        VisitorEnrichmentUpdate(name=valid_name),
    )


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
    visitor_service = VisitorService(db)

    # Verify tenant exists
    tenant_service = TenantService(db)
    tenant = tenant_service.get_tenant_by_id(session_data.tenant_id, include_relations=False)

    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    # Convert SessionCreateRequest to ConversationSessionCreate
    visitor_id, visitor_session_id = _resolve_bootstrapped_visitor_context(
        visitor_service,
        session_data.tenant_id,
        session_data.metadata,
    )
    customer_user_id = _resolve_customer_user_id(session_data.metadata)
    session_create = ConversationSessionCreate(
        tenant_id=session_data.tenant_id,
        user_identifier=session_data.user_identifier,
        visitor_id=visitor_id,
        visitor_session_id=visitor_session_id,
        customer_user_id=customer_user_id,
        meta=session_data.metadata or {},
    )

    session = conversation_service.create_session(session_create)

    return session


@router.post(
    "/message",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(chat_rate_limit)],
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
    automation_orchestrator = ChatAutomationOrchestrator(db)
    rag_service = RAGService(db)
    tenant_service = TenantService(db)
    visitor_service = VisitorService(db)

    logger.info("Processing chat message")

    # Step 1: Get or create session
    session_id = message_data.session_id
    session: SessionResponse | None
    if not session_id:
        # Create new session if not provided
        if not message_data.tenant_id or not message_data.user_identifier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id and user_identifier are required for new sessions",
            )

        visitor_id, visitor_session_id = _resolve_bootstrapped_visitor_context(
            visitor_service,
            message_data.tenant_id,
            message_data.metadata,
        )
        customer_user_id = _resolve_customer_user_id(message_data.metadata)

        # If this public/browser visitor already has an active or escalated thread,
        # continue that thread instead of opening a parallel conversation.
        reused_session: SessionResponse | None = None
        if customer_user_id:
            reused_session = conversation_service.get_latest_session_for_customer(
                tenant_id=message_data.tenant_id,
                customer_user_id=customer_user_id,
                allowed_statuses=[
                    SessionStatus.ACTIVE,
                    SessionStatus.ESCALATED,
                    SessionStatus.RESOLVED,
                ],
            )
        elif visitor_id:
            reused_session = conversation_service.get_latest_session_for_visitor(
                tenant_id=message_data.tenant_id,
                visitor_id=visitor_id,
                allowed_statuses=[SessionStatus.ACTIVE, SessionStatus.ESCALATED],
            )

        if reused_session:
            session = reused_session
            session_id = reused_session.id
            existing_session_model = db.execute(
                select(ConversationSession).where(ConversationSession.id == session_id)
            ).scalar_one_or_none()
            if existing_session_model and visitor_session_id:
                existing_session_model.visitor_session_id = visitor_session_id
                db.commit()
            logger.info("Reusing existing visitor session")
        else:
            # Check conversation limit before creating new session
            capabilities = tenant_service.get_capabilities(message_data.tenant_id)

            if capabilities and capabilities.max_conversations_per_month is not None:
                # Get start of current month

                now = utcnow_naive()
                month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

                # Count conversations this month
                monthly_count = (
                    db.query(ConversationSession)
                    .filter(
                        ConversationSession.tenant_id == message_data.tenant_id,
                        ConversationSession.created_at >= month_start,
                    )
                    .count()
                )

                # Check limit
                if monthly_count >= capabilities.max_conversations_per_month:
                    raise HTTPException(
                        status_code=402,
                        detail={
                            "error": "conversation_limit_reached",
                            "message": f"Monthly conversation limit of {capabilities.max_conversations_per_month} reached.",
                            "current_count": monthly_count,
                            "limit": capabilities.max_conversations_per_month,
                            "resets_at": (month_start + timedelta(days=32))
                            .replace(day=1)
                            .isoformat(),
                        },
                    )

            # Convert to ConversationSessionCreate
            session_create = ConversationSessionCreate(
                tenant_id=message_data.tenant_id,
                user_identifier=message_data.user_identifier,
                visitor_id=visitor_id,
                visitor_session_id=visitor_session_id,
                customer_user_id=customer_user_id,
                meta=message_data.metadata or {},
            )
            session = conversation_service.create_session(session_create)
            session_id = session.id
            logger.info("Created new session")
    else:
        # Verify session exists
        session = conversation_service.get_session(session_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        # Auto-reactivate ABANDONED sessions when user returns
        if session.status == SessionStatus.ABANDONED:
            logger.info("Reactivating abandoned session")
            session = conversation_service.update_session(
                session_id, ConversationSessionUpdate(status=SessionStatus.ACTIVE)
            )
            logger.info("Session reactivated from abandoned to active")
        elif session.status == SessionStatus.RESOLVED:
            welcome_triggers = {"start", "hello", "hi", "hey", "welcome", "__init__", "__welcome__"}
            if message_data.message.lower().strip() not in welcome_triggers:
                logger.info("Reopening resolved session after customer reply")
                session = conversation_service.update_session(
                    session_id, ConversationSessionUpdate(status=SessionStatus.ACTIVE)
                )
                logger.info("Session reactivated from resolved to active")

    session_model = db.execute(
        select(ConversationSession).where(ConversationSession.id == session_id)
    ).scalar_one_or_none()
    if not session_model:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Step 2: Get tenant and AI configuration
    tenant = tenant_service.get_tenant_by_id(session.tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    tenant_response = cast(TenantResponse, tenant)

    # Check subscription status (trial expiration, etc.)
    tenant_model = tenant_service.get_tenant(session.tenant_id)
    if tenant_model:
        # Check trial expiration
        if tenant_model.subscription_status in {"trialing", "trial"}:
            now = utcnow_aware()
            trial_ends_at = ensure_utc_aware(tenant_model.trial_ends_at)
            if trial_ends_at and now > trial_ends_at:
                # Auto-update status
                tenant_model.subscription_status = "trial_expired"
                db.commit()

                raise HTTPException(
                    status_code=402,  # Payment Required
                    detail={
                        "error": "trial_expired",
                        "message": "Your 30-day trial has expired. Please upgrade to continue using Luran.",
                        "trial_ended": trial_ends_at.isoformat(),
                        "upgrade_url": "/dashboard/upgrade",
                    },
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
                },
            )

    # Fetch business profile for chat grounding (one lookup per request; None when absent)
    business_profile = db.execute(
        select(BusinessProfile).where(BusinessProfile.tenant_id == session.tenant_id)
    ).scalar_one_or_none()

    tenant_config = tenant_service.get_ai_config(session.tenant_id)
    if not tenant_config:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant AI configuration not found",
        )
    tenant_ai_config = db.execute(
        select(TenantAIConfig).where(TenantAIConfig.tenant_id == session.tenant_id)
    ).scalar_one_or_none()
    if not tenant_ai_config:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant AI configuration not found",
        )
    require_email_before_escalation = bool(
        getattr(tenant_ai_config, "require_email_before_escalation", False)
    )
    require_name_before_escalation = bool(
        getattr(tenant_ai_config, "require_name_before_escalation", False)
    )

    # Load sales agent config (may be None if tenant has not configured it).
    sales_agent_config = db.execute(
        select(TenantSalesAgentConfig).where(
            TenantSalesAgentConfig.tenant_id == session.tenant_id
        )
    ).scalar_one_or_none()
    _sales_mode = bool(
        sales_agent_config is not None
        and getattr(sales_agent_config, "agent_enabled", False)
        and getattr(sales_agent_config, "lead_capture_enabled", False)
    )
    linked_visitor = visitor_service.get_visitor_for_session(session_id)
    current_session_meta = dict(session_model.meta or {})
    customer_name = _resolve_customer_name(
        current_session_meta,
        message_data.metadata,
        linked_visitor.name if linked_visitor else None,
    )
    customer_contact_email = _resolve_customer_contact_email(
        current_session_meta,
        message_data.metadata,
        linked_visitor.email if linked_visitor else None,
    )
    if customer_name and current_session_meta.get("contact_name") != customer_name:
        current_session_meta["contact_name"] = customer_name
        conversation_service.update_session_meta(session_id, current_session_meta)
        _persist_contact_name_to_visitor(visitor_service, session_id, customer_name)
        session_model.meta = current_session_meta
    if (
        customer_contact_email
        and current_session_meta.get("contact_email") != customer_contact_email
    ):
        current_session_meta["contact_email"] = customer_contact_email
        conversation_service.update_session_meta(session_id, current_session_meta)
        _persist_contact_email_to_visitor(visitor_service, session_id, customer_contact_email)
        session_model.meta = current_session_meta

    # Step 2.5: Check for Flow Execution (BEFORE storing user message)
    flow_engine = FlowEngine(db)
    button_value: str | None = None
    if isinstance(message_data.metadata, dict):
        raw_button_value = message_data.metadata.get("button_value")
        if isinstance(raw_button_value, str) and raw_button_value.strip():
            button_value = raw_button_value.strip()

    effective_message = button_value or message_data.message
    effective_message_lower = effective_message.lower().strip()
    session_meta = dict(session_model.meta or {})
    rag_only_mode = bool(session_meta.get("rag_only_mode", False))

    # Global start router: if no active execution and at least one active flow,
    # present flow choices instead of relying on overlapping keywords.
    forced_flow: Flow | None = None
    active_exec = flow_engine._get_active_execution(session_model.id)
    if not active_exec:
        if effective_message_lower == SYSTEM_START_TRIGGER:
            router_flows = flow_engine.list_router_flows(session_model.tenant_id)
            if len(router_flows) >= 1:
                actions = flow_engine.build_router_actions(router_flows)
                labels = [action["label"] for action in actions]
                router_message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content="How can we help you today? Please choose an option:",
                    meta={
                        "flow_router": True,
                        "flow_triggered": True,
                        "buttons": labels,
                        "button_actions": actions,
                    },
                )
                return ChatResponse(
                    session_id=session_id,
                    message=router_message,
                    should_escalate=False,
                    escalation_reason=None,
                    ticket_id=None,
                )
        elif effective_message_lower.startswith(FLOW_START_PREFIX):
            selected_flow_id = effective_message.split(":", 1)[-1].strip()
            try:
                selected_flow_uuid = UUID(selected_flow_id)
            except ValueError:
                selected_flow_uuid = None

            if selected_flow_uuid:
                forced_flow = db.execute(
                    select(Flow).where(
                        Flow.id == selected_flow_uuid,
                        Flow.tenant_id == session_model.tenant_id,
                        Flow.is_active.is_(True),
                        Flow.status == FlowStatus.ACTIVE,
                    )
                ).scalar_one_or_none()
                if forced_flow and rag_only_mode:
                    session_meta["rag_only_mode"] = False
                    session_model.meta = session_meta
                    db.commit()
        elif effective_message_lower == RAG_CHAT_TOKEN:
            if not rag_only_mode:
                session_meta["rag_only_mode"] = True
                session_model.meta = session_meta
                db.commit()
            assistant_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content="You are now chatting with our support assistant. Ask me anything.",
                meta={"flow_router": False, "rag_mode": True},
            )
            return ChatResponse(
                session_id=session_id,
                message=assistant_message,
                should_escalate=False,
                escalation_reason=None,
                ticket_id=None,
            )

        elif effective_message_lower == TALK_TO_HUMAN_TOKEN:
            router_escalation_reason = "User selected talk to human from start menu"
            session_meta = dict(session_model.meta or {})
            contact_name = _resolve_customer_name(
                session_meta,
                message_data.metadata,
                linked_visitor.name if linked_visitor else None,
            )
            contact_email = _resolve_customer_contact_email(
                session_meta,
                message_data.metadata,
                linked_visitor.email if linked_visitor else None,
            )
            missing_contact_fields = _get_missing_contact_fields(
                require_name_before_escalation=require_name_before_escalation,
                require_email_before_escalation=require_email_before_escalation,
                customer_name=contact_name,
                customer_contact_email=contact_email,
            )
            if missing_contact_fields:
                _store_pending_contact_capture(
                    conversation_service=conversation_service,
                    session_id=session_id,
                    session_meta=session_meta,
                    escalation_reason=router_escalation_reason,
                    customer_query=message_data.message,
                    required_fields=missing_contact_fields,
                )
                assistant_message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=_build_contact_capture_prompt(missing_contact_fields),
                    meta={
                        "contact_capture_required": True,
                        "contact_capture_fields": missing_contact_fields,
                    },
                )
                return ChatResponse(
                    session_id=session_id,
                    message=assistant_message,
                    should_escalate=False,
                    escalation_reason=None,
                    ticket_id=None,
                )
            conversation_service.escalate_session(
                session_id=session_id, reason=router_escalation_reason
            )
            ticket_id = None
            try:

                ticket_service = TicketService(db)
                ticket = ticket_service.create_ticket(
                    TicketCreate(
                        tenant_id=session.tenant_id,
                        session_id=session_id,
                        priority=TicketPriority.HIGH,
                        summary="User selected Talk to an Agent from flow start menu.",
                    )
                )
                ticket_id = ticket.id

                # Notify support/admin team via email
                tenant_for_email = tenant_service.get_tenant_by_id(
                    session.tenant_id, include_relations=False
                )
                tenant_name = tenant_for_email.display_name if tenant_for_email else "Unknown"
                recipients = (
                    db.query(User)
                    .filter(
                        User.tenant_id == session.tenant_id,
                        User.is_active.is_(True),
                        User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                    )
                    .all()
                )
                for recipient in recipients:
                    try:
                        await send_ticket_escalation_email(
                            email_to=recipient.email,
                            user_name=recipient.full_name,
                            tenant_name=tenant_name,
                            ticket_number=ticket.ticket_number,
                            escalation_reason=router_escalation_reason,
                            customer_query=message_data.message,
                            ticket_priority=TicketPriority.HIGH.value.upper(),
                            ticket_id=str(ticket.id),
                            customer_contact_email=contact_email,
                        )
                    except Exception as email_error:
                        logger.error(
                            f"Router escalation email error for {recipient.email}: {email_error}"
                        )
            except Exception as ticket_error:
                logger.error(f"Failed to create router escalation ticket: {ticket_error}")
            assistant_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content="I am connecting you with a human support agent now.",
                meta={"flow_triggered": True, "flow_router": False},
            )
            return ChatResponse(
                session_id=session_id,
                message=assistant_message,
                should_escalate=True,
                escalation_reason=router_escalation_reason,
                ticket_id=ticket_id,
            )

    if rag_only_mode and not forced_flow:
        matched_flow = None
    else:
        matched_flow = forced_flow or flow_engine.match_flow(effective_message, session_model)

    if matched_flow:
        logger.info(f"Matched flow: {matched_flow.name} (ID: {matched_flow.id})")

        def _extract_flow_rag(collected_data: dict) -> tuple[list, list]:
            rag_data = collected_data.get("rag_response") if collected_data else None
            if isinstance(rag_data, dict) and rag_data.get("chunks"):
                chunks = [
                    SimpleNamespace(id=chunk.get("id"), content=chunk.get("content"))
                    for chunk in rag_data.get("chunks", [])
                ]
                scores = rag_data.get("scores", [])
                return chunks, scores
            return [], []

        # Get or create flow execution
        active_exec = flow_engine._get_active_execution(session.id)

        if not active_exec:
            # Start new flow execution

            active_exec = FlowExecution(
                flow_id=matched_flow.id,
                session_id=session.id,
                current_node_id=matched_flow.nodes.get("start_node_id", "start"),
                status="pending",
                collected_data={},
                started_at=utcnow_naive(),
            )
            db.add(active_exec)
            db.commit()
            db.refresh(active_exec)
            logger.info(f"Created new flow execution: {active_exec.id}")

        # Execute current node. For explicit manual flow starts, do not pass the
        # hidden router token into the first interactive node as user input.
        flow_input = None if forced_flow else effective_message
        flow_result = await flow_engine.execute_node(active_exec, flow_input)
        flow_response = None
        flow_handoff_to_rag = False

        if flow_result.get("is_complete"):
            # Flow finished
            if flow_result.get("fallback_to_rag"):
                # User input didn't match flow expectations, fall through to RAG
                logger.info("Flow completed with fallback to RAG")
                flow_handoff_to_rag = True
            elif flow_result.get("should_escalate"):
                # Flow triggered escalation - store user message first
                user_message = conversation_service.add_message(
                    session_id=session_id, role=MessageRole.USER, content=message_data.message
                )

                flow_escalation_reason = flow_result.get(
                    "escalation_reason", "Flow triggered escalation"
                )
                session_meta = dict(session_model.meta or {})
                contact_name = _resolve_customer_name(
                    session_meta,
                    message_data.metadata,
                    linked_visitor.name if linked_visitor else None,
                )
                contact_email = _resolve_customer_contact_email(
                    session_meta,
                    message_data.metadata,
                    linked_visitor.email if linked_visitor else None,
                )
                missing_contact_fields = _get_missing_contact_fields(
                    require_name_before_escalation=require_name_before_escalation,
                    require_email_before_escalation=require_email_before_escalation,
                    customer_name=contact_name,
                    customer_contact_email=contact_email,
                )
                if missing_contact_fields:
                    _store_pending_contact_capture(
                        conversation_service=conversation_service,
                        session_id=session_id,
                        session_meta=session_meta,
                        escalation_reason=flow_escalation_reason,
                        customer_query=message_data.message,
                        required_fields=missing_contact_fields,
                    )
                    assistant_message = conversation_service.add_message(
                        session_id=session_id,
                        role=MessageRole.ASSISTANT,
                        content=_build_contact_capture_prompt(missing_contact_fields),
                        meta={
                            "contact_capture_required": True,
                            "contact_capture_fields": missing_contact_fields,
                            "flow_triggered": True,
                        },
                    )
                    return ChatResponse(
                        session_id=session_id,
                        message=assistant_message,
                        should_escalate=False,
                        escalation_reason=None,
                        ticket_id=None,
                    )
                conversation_service.escalate_session(
                    session_id=session_id, reason=flow_escalation_reason
                )

                # Add flow response message
                response_message = flow_result.get("message", "Connecting you to an agent...")
                assistant_message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=response_message,
                    meta={"flow_triggered": True, "flow_id": str(matched_flow.id)},
                )

                # Create ticket
                ticket_id = None
                try:

                    ticket_service = TicketService(db)
                    conversation_history = conversation_service.get_session_messages(
                        session_id=session_id, limit=20
                    )

                    summary = _generate_ticket_summary(
                        conversation_history=conversation_history,
                        escalation_reason=flow_escalation_reason,
                        user_query=message_data.message,
                    )

                    priority = _determine_ticket_priority(
                        user_query=message_data.message,
                        escalation_reason=flow_escalation_reason,
                    )

                    ticket_data = TicketCreate(
                        tenant_id=session.tenant_id,
                        session_id=session_id,
                        priority=priority,
                        summary=summary,
                    )
                    ticket = ticket_service.create_ticket(ticket_data)
                    ticket_id = ticket.id
                    logger.info(f"Ticket created from flow: {ticket.ticket_number}")

                    # Send email notifications
                    tenant_for_email = tenant_service.get_tenant_by_id(
                        session.tenant_id, include_relations=False
                    )
                    tenant_name = tenant_for_email.display_name if tenant_for_email else "Unknown"

                    notification_recipients = (
                        db.query(User)
                        .filter(
                            User.tenant_id == session.tenant_id,
                            User.is_active.is_(True),
                            User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                        )
                        .all()
                    )

                    for recipient in notification_recipients:
                        try:
                            email_sent = await send_ticket_escalation_email(
                                email_to=recipient.email,
                                user_name=recipient.full_name,
                                tenant_name=tenant_name,
                                ticket_number=ticket.ticket_number,
                                escalation_reason=flow_escalation_reason,
                                customer_query=message_data.message,
                                ticket_priority=priority.value.upper(),
                                ticket_id=str(ticket.id),
                                customer_contact_email=contact_email,
                            )
                            if email_sent:
                                logger.info("Flow escalation email sent")
                        except Exception:
                            logger.error("Flow escalation email delivery failed")

                except Exception as ticket_error:
                    logger.error(f"Failed to create ticket from flow: {ticket_error}")

                flow_response = ChatResponse(
                    session_id=session_id,
                    message=assistant_message,
                    should_escalate=True,
                    escalation_reason=flow_escalation_reason,
                    ticket_id=ticket_id,
                )
            elif flow_result.get("use_rag_response"):
                # Flow called RAG, use retrieved chunks if available
                if flow_result.get("chunks"):
                    # Store user message first
                    user_message = conversation_service.add_message(
                        session_id=session_id, role=MessageRole.USER, content=message_data.message
                    )

                    # Use chunks from flow for AI generation
                    retrieved_chunks = flow_result["chunks"]
                    similarity_scores = flow_result.get("scores", [])

                    # Continue to AI generation with these chunks (skip vector retrieval)
                    # This is handled below - set flag
                    flow_rag_chunks = retrieved_chunks
                    flow_rag_scores = similarity_scores
                    logger.info(f"Using RAG chunks from flow: {len(retrieved_chunks)} chunks")
                    flow_handoff_to_rag = True
                else:
                    # Try to use RAG chunks from collected data
                    flow_rag_chunks, flow_rag_scores = _extract_flow_rag(active_exec.collected_data)
                    if flow_rag_chunks:
                        flow_handoff_to_rag = True
                        logger.info(
                            f"Using RAG chunks from flow execution: {len(flow_rag_chunks)} chunks"
                        )
                    else:
                        # END node with use_rag_response but no chunks - fall through to normal RAG
                        logger.info(
                            "Flow completed with use_rag_response flag, continuing to normal RAG"
                        )
                        flow_handoff_to_rag = True
            else:
                # Flow returned message, store user message and return flow response
                user_message = conversation_service.add_message(
                    session_id=session_id, role=MessageRole.USER, content=message_data.message
                )

                assistant_message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=flow_result["message"],
                    meta={
                        "flow_triggered": True,
                        "flow_id": str(matched_flow.id),
                        "flow_completed": True,
                        "flow_output": flow_result.get("structured_output"),
                    },
                )

                flow_response = ChatResponse(
                    session_id=session_id,
                    message=assistant_message,
                    should_escalate=False,
                    escalation_reason=None,
                    ticket_id=None,
                )
        else:
            # Flow still running, store user message and return flow message
            user_message = conversation_service.add_message(
                session_id=session_id, role=MessageRole.USER, content=message_data.message
            )

            assistant_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=flow_result["message"],
                meta={
                    "flow_triggered": True,
                    "flow_id": str(matched_flow.id),
                    "flow_node": active_exec.current_node_id,
                    "buttons": flow_result.get("buttons"),
                    "button_actions": flow_result.get("button_actions"),
                    "flow_output": flow_result.get("structured_output"),
                },
            )

            # Check if we should continue execution (for auto-executing nodes)
            if flow_result.get("continue_execution"):
                logger.info("Continuing flow execution to next node...")
                # Execute next node immediately
                continued_result = await flow_engine.execute_node(active_exec, None)

                # If continued result has a message, send it
                if continued_result.get("message"):
                    continued_message = conversation_service.add_message(
                        session_id=session_id,
                        role=MessageRole.ASSISTANT,
                        content=continued_result["message"],
                        meta={
                            "flow_triggered": True,
                            "flow_id": str(matched_flow.id),
                            "flow_node": active_exec.current_node_id,
                            "flow_output": continued_result.get("structured_output"),
                        },
                    )
                    assistant_message = continued_message

                if continued_result.get("should_escalate"):
                    continued_escalation_reason = continued_result.get(
                        "escalation_reason", "Flow triggered escalation"
                    )
                    session_meta = dict(session_model.meta or {})
                    contact_name = _resolve_customer_name(
                        session_meta,
                        message_data.metadata,
                        linked_visitor.name if linked_visitor else None,
                    )
                    contact_email = _resolve_customer_contact_email(
                        session_meta,
                        message_data.metadata,
                        linked_visitor.email if linked_visitor else None,
                    )
                    missing_contact_fields = _get_missing_contact_fields(
                        require_name_before_escalation=require_name_before_escalation,
                        require_email_before_escalation=require_email_before_escalation,
                        customer_name=contact_name,
                        customer_contact_email=contact_email,
                    )
                    if missing_contact_fields:
                        _store_pending_contact_capture(
                            conversation_service=conversation_service,
                            session_id=session_id,
                            session_meta=session_meta,
                            escalation_reason=continued_escalation_reason,
                            customer_query=message_data.message,
                            required_fields=missing_contact_fields,
                        )
                        assistant_message = conversation_service.add_message(
                            session_id=session_id,
                            role=MessageRole.ASSISTANT,
                            content=_build_contact_capture_prompt(missing_contact_fields),
                            meta={
                                "contact_capture_required": True,
                                "contact_capture_fields": missing_contact_fields,
                                "flow_triggered": True,
                            },
                        )
                        return ChatResponse(
                            session_id=session_id,
                            message=assistant_message,
                            should_escalate=False,
                            escalation_reason=None,
                            ticket_id=None,
                        )
                    conversation_service.escalate_session(
                        session_id=session_id, reason=continued_escalation_reason
                    )

                    response_message = continued_result.get(
                        "message", "Connecting you to an agent..."
                    )
                    assistant_message = conversation_service.add_message(
                        session_id=session_id,
                        role=MessageRole.ASSISTANT,
                        content=response_message,
                        meta={"flow_triggered": True, "flow_id": str(matched_flow.id)},
                    )

                    # Create ticket
                    ticket_id = None
                    try:

                        ticket_service = TicketService(db)
                        conversation_history = conversation_service.get_session_messages(
                            session_id=session_id, limit=20
                        )

                        summary = _generate_ticket_summary(
                            conversation_history=conversation_history,
                            escalation_reason=continued_escalation_reason,
                            user_query=message_data.message,
                        )

                        priority = _determine_ticket_priority(
                            user_query=message_data.message,
                            escalation_reason=continued_escalation_reason,
                        )

                        ticket_data = TicketCreate(
                            tenant_id=session.tenant_id,
                            session_id=session_id,
                            priority=priority,
                            summary=summary,
                        )
                        ticket = ticket_service.create_ticket(ticket_data)
                        ticket_id = ticket.id
                        logger.info(f"Ticket created from flow: {ticket.ticket_number}")

                        # Send email notifications
                        tenant_for_email = tenant_service.get_tenant_by_id(
                            session.tenant_id, include_relations=False
                        )
                        tenant_name = (
                            tenant_for_email.display_name if tenant_for_email else "Unknown"
                        )

                        notification_recipients = (
                            db.query(User)
                            .filter(
                                User.tenant_id == session.tenant_id,
                                User.is_active.is_(True),
                                User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                            )
                            .all()
                        )

                        for recipient in notification_recipients:
                            try:
                                email_sent = await send_ticket_escalation_email(
                                    email_to=recipient.email,
                                    user_name=recipient.full_name,
                                    tenant_name=tenant_name,
                                    ticket_number=ticket.ticket_number,
                                    escalation_reason=continued_escalation_reason,
                                    customer_query=message_data.message,
                                    ticket_priority=priority.value.upper(),
                                    ticket_id=str(ticket.id),
                                    customer_contact_email=contact_email,
                                )
                                if email_sent:
                                    logger.info("Flow escalation email sent")
                            except Exception:
                                logger.error("Flow escalation email delivery failed")

                    except Exception as ticket_error:
                        logger.error(f"Failed to create ticket from flow: {ticket_error}")

                    return ChatResponse(
                        session_id=session_id,
                        message=assistant_message,
                        should_escalate=True,
                        escalation_reason=continued_escalation_reason,
                        ticket_id=ticket_id,
                    )

                if continued_result.get("use_rag_response"):
                    flow_rag_chunks, flow_rag_scores = _extract_flow_rag(active_exec.collected_data)
                    flow_handoff_to_rag = True

            flow_response = ChatResponse(
                session_id=session_id,
                message=assistant_message,
                should_escalate=False,
                escalation_reason=None,
                ticket_id=None,
            )

        if flow_response and not flow_handoff_to_rag:
            return flow_response
        if flow_handoff_to_rag:
            logger.info("Flow handed off to RAG pipeline")
    else:
        # No flow matched - check if this is a welcome/start trigger
        # If so, return default welcome message without storing trigger word
        welcome_triggers = {"start", "hello", "hi", "hey", "welcome", "__init__", "__welcome__"}
        message_lower = effective_message_lower

        if message_lower in welcome_triggers:
            logger.info(
                f"No flow matched welcome trigger '{message_data.message}', returning default welcome"
            )

            # Return default welcome message without storing the trigger word
            tenant_for_welcome = tenant_service.get_tenant_by_id(
                session.tenant_id, include_relations=False
            )
            company_name = tenant_for_welcome.display_name if tenant_for_welcome else "our team"

            default_welcome = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=f"Hi! Welcome to {company_name}. I'm here to help! What can I assist you with today?",
                meta={"auto_welcome": True, "no_flow_available": True},
            )

            return ChatResponse(
                session_id=session_id,
                message=default_welcome,
                should_escalate=False,
                escalation_reason=None,
                ticket_id=None,
            )
        # If not a welcome trigger, continue to normal RAG processing

    # Step 3: Check for pending escalation (consent-based escalation)
    session_meta = session.meta or {}
    pending_contact_capture = bool(session_meta.get("pending_contact_capture", False))
    if pending_contact_capture:
        contact_capture_context = session_meta.get("contact_capture_context", {}) or {}
        required_fields = (
            contact_capture_context.get("required_fields")
            if isinstance(contact_capture_context, dict)
            else None
        )
        if not isinstance(required_fields, list) or not required_fields:
            required_fields = ["email"]

        current_field = required_fields[0]
        captured_value = (
            _extract_customer_name(message_data.message)
            if current_field == "name"
            else _extract_valid_email(message_data.message)
        )
        if not captured_value:
            user_message = conversation_service.add_message(
                session_id=session_id, role=MessageRole.USER, content=message_data.message
            )
            assistant_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=_build_contact_capture_retry_prompt(current_field),
                meta={"contact_capture_required": True, "contact_capture_fields": required_fields},
            )
            return ChatResponse(
                session_id=session_id,
                message=assistant_message,
                should_escalate=False,
                escalation_reason=None,
                ticket_id=None,
            )

        if current_field == "name":
            session_meta["contact_name"] = captured_value
            _persist_contact_name_to_visitor(visitor_service, session_id, captured_value)
        else:
            session_meta["contact_email"] = captured_value
            _persist_contact_email_to_visitor(visitor_service, session_id, captured_value)

        remaining_fields = required_fields[1:]
        session_meta["pending_contact_capture"] = bool(remaining_fields)
        if isinstance(contact_capture_context, dict):
            contact_capture_context["required_fields"] = remaining_fields
            session_meta["contact_capture_context"] = (
                contact_capture_context if remaining_fields else {}
            )
        else:
            session_meta["contact_capture_context"] = {}
        conversation_service.update_session_meta(session_id, session_meta)

        user_message = conversation_service.add_message(
            session_id=session_id, role=MessageRole.USER, content=message_data.message
        )

        if remaining_fields:
            assistant_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=_build_contact_capture_prompt(remaining_fields),
                meta={"contact_capture_required": True, "contact_capture_fields": remaining_fields},
            )
            return ChatResponse(
                session_id=session_id,
                message=assistant_message,
                should_escalate=False,
                escalation_reason=None,
                ticket_id=None,
            )

        capture_escalation_reason = (
            contact_capture_context.get("reason")
            if isinstance(contact_capture_context, dict)
            else None
        ) or "Escalation requested"
        customer_query = (
            contact_capture_context.get("customer_query")
            if isinstance(contact_capture_context, dict)
            else None
        ) or message_data.message

        conversation_service.escalate_session(
            session_id=session_id, reason=capture_escalation_reason
        )

        assistant_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content="Thanks. Connecting you with our support team now.",
            meta={"contact_captured": True},
        )

        final_contact_email = _resolve_customer_contact_email(session_meta, message_data.metadata)

        ticket_id = None
        try:
            ticket_service = TicketService(db)
            conversation_history = conversation_service.get_session_messages(
                session_id=session_id, limit=20
            )

            summary = _generate_ticket_summary(
                conversation_history=conversation_history,
                escalation_reason=capture_escalation_reason,
                user_query=customer_query,
            )

            priority = _determine_ticket_priority(
                user_query=customer_query,
                escalation_reason=capture_escalation_reason,
            )

            ticket_data = TicketCreate(
                tenant_id=session.tenant_id,
                session_id=session_id,
                priority=priority,
                summary=summary,
            )
            ticket = ticket_service.create_ticket(ticket_data)
            ticket_id = ticket.id

            tenant_for_email = tenant_service.get_tenant_by_id(
                session.tenant_id, include_relations=False
            )
            tenant_name = tenant_for_email.display_name if tenant_for_email else "Unknown"

            notification_recipients = (
                db.query(User)
                .filter(
                    User.tenant_id == session.tenant_id,
                    User.is_active.is_(True),
                    User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                )
                .all()
            )

            for recipient in notification_recipients:
                try:
                    await send_ticket_escalation_email(
                        email_to=recipient.email,
                        user_name=recipient.full_name,
                        tenant_name=tenant_name,
                        ticket_number=ticket.ticket_number,
                        escalation_reason=capture_escalation_reason,
                        customer_query=customer_query,
                        ticket_priority=priority.value.upper(),
                        ticket_id=str(ticket.id),
                        customer_contact_email=final_contact_email,
                    )
                except Exception:
                    logger.error("Contact-capture escalation email delivery failed")
        except Exception as ticket_error:
            logger.error(f"Failed to create ticket after contact capture: {ticket_error}")

        return ChatResponse(
            session_id=session_id,
            message=assistant_message,
            should_escalate=True,
            escalation_reason=capture_escalation_reason,
            ticket_id=ticket_id,
        )

    pending_escalation = session_meta.get("pending_escalation", False)
    escalation_context = session_meta.get("escalation_context", {})

    if pending_escalation:
        # User was asked if they want escalation - check their response
        consent_decision = rag_service.detect_escalation_consent(message_data.message)

        if consent_decision == "consent":
            # User agreed to escalation - escalate now
            logger.info("User consented to escalation")

            # Add user message
            user_message = conversation_service.add_message(
                session_id=session_id, role=MessageRole.USER, content=message_data.message
            )

            consent_escalation_reason = escalation_context.get(
                "reason", "User consented to escalation"
            )
            consent_original_query = escalation_context.get("original_query", message_data.message)
            contact_name = _resolve_customer_name(
                session_meta,
                message_data.metadata,
                linked_visitor.name if linked_visitor else None,
            )
            contact_email = _resolve_customer_contact_email(
                session_meta,
                message_data.metadata,
                linked_visitor.email if linked_visitor else None,
            )
            missing_contact_fields = _get_missing_contact_fields(
                require_name_before_escalation=require_name_before_escalation,
                require_email_before_escalation=require_email_before_escalation,
                customer_name=contact_name,
                customer_contact_email=contact_email,
            )

            if missing_contact_fields:
                session_meta["pending_escalation"] = False
                session_meta["escalation_context"] = {}
                _store_pending_contact_capture(
                    conversation_service=conversation_service,
                    session_id=session_id,
                    session_meta=session_meta,
                    escalation_reason=consent_escalation_reason,
                    customer_query=consent_original_query,
                    required_fields=missing_contact_fields,
                )
                assistant_message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=_build_contact_capture_prompt(missing_contact_fields),
                    meta={
                        "contact_capture_required": True,
                        "contact_capture_fields": missing_contact_fields,
                    },
                )
                return ChatResponse(
                    session_id=session_id,
                    message=assistant_message,
                    should_escalate=False,
                    escalation_reason=None,
                    ticket_id=None,
                )

            if contact_name and session_meta.get("contact_name") != contact_name:
                session_meta["contact_name"] = contact_name
            if contact_email and session_meta.get("contact_email") != contact_email:
                session_meta["contact_email"] = contact_email

            # Clear pending escalation from session
            session_meta["pending_escalation"] = False
            session_meta["escalation_context"] = {}
            conversation_service.update_session_meta(session_id, session_meta)
            _persist_contact_name_to_visitor(visitor_service, session_id, contact_name)
            _persist_contact_email_to_visitor(visitor_service, session_id, contact_email)

            # Escalate session
            conversation_service.escalate_session(
                session_id=session_id, reason=consent_escalation_reason
            )

            # Create ticket (using existing logic below)
            # Add assistant confirmation message
            confirmation_text = "Perfect! Connecting you with someone from our team now.\n\nSomeone from our support team will be with you shortly."
            assistant_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=confirmation_text,
                meta={"escalation_confirmed": True},
            )

            # Create ticket (copy logic from below)
            ticket_id = None
            try:

                ticket_service = TicketService(db)
                conversation_history = conversation_service.get_session_messages(
                    session_id=session_id, limit=20
                )

                summary = _generate_ticket_summary(
                    conversation_history=conversation_history,
                    escalation_reason=consent_escalation_reason,
                    user_query=consent_original_query,
                )

                priority = _determine_ticket_priority(
                    user_query=consent_original_query,
                    escalation_reason=consent_escalation_reason,
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
                tenant_for_email = tenant_service.get_tenant_by_id(
                    session.tenant_id, include_relations=False
                )
                tenant_name = tenant_for_email.display_name if tenant_for_email else "Unknown"

                notification_recipients = (
                    db.query(User)
                    .filter(
                        User.tenant_id == session.tenant_id,
                        User.is_active.is_(True),
                        User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                    )
                    .all()
                )

                for recipient in notification_recipients:
                    try:
                        email_sent = await send_ticket_escalation_email(
                            email_to=recipient.email,
                            user_name=recipient.full_name,
                            tenant_name=tenant_name,
                            ticket_number=ticket.ticket_number,
                            escalation_reason=consent_escalation_reason,
                            customer_query=consent_original_query,
                            ticket_priority=priority.value.upper(),
                            ticket_id=str(ticket.id),
                            customer_contact_email=contact_email,
                        )
                        if email_sent:
                            logger.info("Escalation email sent")
                    except Exception:
                        logger.error("Escalation email delivery failed")

            except Exception as ticket_error:
                logger.error(f"Failed to create ticket: {ticket_error}")

            return ChatResponse(
                session_id=session_id,
                message=assistant_message,
                should_escalate=True,
                escalation_reason=consent_escalation_reason,
                ticket_id=ticket_id,
            )

        elif consent_decision == "reject":
            # User declined escalation - check if they also asked a new question
            logger.info("User declined escalation offer")

            # Clear pending escalation
            session_meta["pending_escalation"] = False
            session_meta["escalation_context"] = {}
            conversation_service.update_session_meta(session_id, session_meta)

            # Check if message is a simple rejection or has additional content
            message_words = message_data.message.strip().split()
            is_simple_rejection = len(message_words) <= 3  # "no", "nope", "no thanks"

            # If simple rejection, send acknowledgment and return
            if is_simple_rejection:
                # Add user message
                user_message = conversation_service.add_message(
                    session_id=session_id, role=MessageRole.USER, content=message_data.message
                )

                # Send acknowledgment
                ack_text = "Sounds good! Let me know if you need anything else."
                assistant_message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=ack_text,
                    meta={"escalation_declined": True},
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
                logger.info(
                    f"Rejection contains additional content ({len(message_words)} words), treating as new question"
                )
        else:
            # Unclear response - treat as normal question and try to answer
            logger.info("Unclear consent response, treating as new question")
            # Clear pending escalation and continue to normal flow
            session_meta["pending_escalation"] = False
            session_meta["escalation_context"] = {}
            conversation_service.update_session_meta(session_id, session_meta)

    # Step 4: Add user message to database
    user_message = conversation_service.add_message(
        session_id=session_id, role=MessageRole.USER, content=message_data.message
    )
    logger.info(f"Added user message: {user_message.id}")

    try:
        conversation_history = conversation_service.get_session_messages(
            session_id=session_id,
            limit=20,
        )
        automation_result = await automation_orchestrator.handle_turn(
            tenant_id=session.tenant_id,
            session_id=session_id,
            user_message=message_data.message,
            session_meta=dict(session_model.meta or {}),
            conversation_history=conversation_history,
            tenant_ai_config=tenant_ai_config,
            company_name=tenant_response.display_name,
        )
        if automation_result.handled and automation_result.assistant_text:
            assistant_message = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=automation_result.assistant_text,
                token_count=automation_result.token_count,
                meta=automation_result.metadata or {},
            )
            return ChatResponse(
                session_id=session_id,
                message=assistant_message,
                should_escalate=automation_result.should_escalate,
                escalation_reason=automation_result.escalation_reason,
                ticket_id=None,
            )

        # Step 4: Retrieve relevant knowledge chunks via vector search
        pending_contact_capture_reason: str | None = None
        pending_contact_capture_query: str | None = None
        pending_contact_capture_fields: list[str] | None = None

        # Check if flow provided RAG chunks
        if "flow_rag_chunks" in locals() and flow_rag_chunks:
            retrieved_chunks = flow_rag_chunks
            similarity_scores = flow_rag_scores
            logger.info(f"Using flow-provided RAG chunks: {len(retrieved_chunks)} chunks")
        else:
            logger.info("Retrieving relevant knowledge chunks...")
            retrieved_chunks, similarity_scores = await rag_service.retrieve_relevant_chunks(
                tenant_id=session.tenant_id,
                query=message_data.message,
                max_chunks=rag_service.MAX_CHUNKS_RETRIEVE,
                score_threshold=rag_service.MIN_SCORE_THRESHOLD,
            )
            logger.info(
                f"Retrieved {len(retrieved_chunks)} chunks with avg similarity: {sum(similarity_scores) / len(similarity_scores):.2f}"
                if similarity_scores
                else f"Retrieved {len(retrieved_chunks)} chunks"
            )

        avg_similarity = (
            (sum(similarity_scores) / len(similarity_scores)) if similarity_scores else 0.0
        )

        # Step 5: Deterministic scope/topic guard before model generation
        scope_allowed, scope_reason = rag_service.enforce_scope_policy(
            user_query=message_data.message,
            ai_config=tenant_ai_config,
            retrieved_chunks_count=len(retrieved_chunks),
            avg_similarity=avg_similarity,
        )

        # Step 5.5: Determine initial escalation strategy
        escalation_decision = rag_service.should_escalate(
            user_query=message_data.message,
            ai_config=tenant_ai_config,
            retrieved_chunks_count=len(retrieved_chunks),
            similarity_scores=similarity_scores,
        )
        logger.info(f"Initial escalation action: {escalation_decision.action.value}")

        # Step 6: Get conversation history for context (needed for ticket creation too)

        # Step 7: Determine directive action (no hardcoded fallback text)
        should_skip_generation = False
        skip_reason = None
        token_count: int | None = None
        prompt_components = None
        policy_action = "answer"
        policy_reason = "normal_generation"
        _sales_meta: dict | None = None  # populated only in sales mode

        if not scope_allowed:
            policy_action = "refuse"
            policy_reason = f"scope_policy_{scope_reason}"
            escalation_decision = EscalationDecision(
                action=EscalationAction.NO_ESCALATION,
                reason=scope_reason,
                skip_quality_check=True,
            )
            logger.info(f"Scope policy blocked request: {scope_reason}")

        if escalation_decision.action == EscalationAction.AUTO_ESCALATE:
            auto_escalation_reason = escalation_decision.reason or "Escalation requested"
            auto_escalation_name = _resolve_customer_name(
                dict(session.meta or {}),
                message_data.metadata,
                linked_visitor.name if linked_visitor else None,
            )
            auto_escalation_contact = _resolve_customer_contact_email(
                dict(session.meta or {}),
                message_data.metadata,
                linked_visitor.email if linked_visitor else None,
            )
            missing_contact_fields = _get_missing_contact_fields(
                require_name_before_escalation=require_name_before_escalation,
                require_email_before_escalation=require_email_before_escalation,
                customer_name=auto_escalation_name,
                customer_contact_email=auto_escalation_contact,
            )
            if missing_contact_fields:
                ai_response_text = _build_contact_capture_prompt(missing_contact_fields)
                token_count = 0
                quality = ResponseQuality(
                    is_substantial=True, has_uncertainty=False, word_count=0, char_count=0
                )
                pending_contact_capture_reason = auto_escalation_reason
                pending_contact_capture_query = message_data.message
                pending_contact_capture_fields = missing_contact_fields
                escalation_decision = EscalationDecision(
                    action=EscalationAction.NO_ESCALATION,
                    reason="awaiting_contact_details_for_escalation",
                    skip_quality_check=True,
                )
                logger.info(
                    f"Auto-escalation deferred: awaiting customer fields {missing_contact_fields}"
                )
            else:
                ai_response_text = "Of course! Connecting you with someone from our team now."
                token_count = 0
                quality = ResponseQuality(
                    is_substantial=True, has_uncertainty=False, word_count=0, char_count=0
                )
                logger.info("Auto-escalating due to user request")

        else:
            grounding_mode = str(getattr(tenant_ai_config, "grounding_mode", "balanced"))
            if scope_allowed:
                low_confidence_action = str(
                    getattr(tenant_ai_config, "low_confidence_action", "clarify")
                )
                no_retrieval_action = str(
                    getattr(tenant_ai_config, "no_retrieval_action", "clarify")
                )

                has_no_retrieval = len(retrieved_chunks) == 0
                has_low_confidence = (
                    len(retrieved_chunks) > 0
                    and avg_similarity < rag_service.MIN_RELEVANT_SIMILARITY
                )
                has_followup_context = _has_usable_followup_context(conversation_history)

                if has_no_retrieval:
                    if has_followup_context:
                        policy_action = "answer"
                        policy_reason = "no_retrieval_followup_context"
                    elif grounding_mode in {"strict", "balanced"}:
                        policy_action = no_retrieval_action
                        policy_reason = "no_retrieval"
                    elif grounding_mode == "flexible" and no_retrieval_action != "clarify":
                        policy_action = no_retrieval_action
                        policy_reason = "no_retrieval"
                elif has_low_confidence:
                    if grounding_mode == "strict":
                        policy_action = low_confidence_action
                        policy_reason = f"low_similarity_{avg_similarity:.2f}"
                    elif grounding_mode == "balanced" and escalation_decision.reason not in [
                        "conversational_mode",
                        "partial_knowledge_mode",
                    ]:
                        policy_action = low_confidence_action
                        policy_reason = f"low_similarity_{avg_similarity:.2f}"
                    elif grounding_mode == "flexible" and low_confidence_action != "clarify":
                        policy_action = low_confidence_action
                        policy_reason = f"low_similarity_{avg_similarity:.2f}"

            if policy_action == "escalate":
                escalation_decision.action = EscalationAction.OFFER_ESCALATION
                escalation_decision.reason = f"Grounding policy: {policy_reason}"
                escalation_decision.skip_quality_check = True
            elif policy_action in {"clarify", "refuse"}:
                escalation_decision.action = EscalationAction.NO_ESCALATION
                escalation_decision.reason = f"Grounding policy: {policy_reason}"
                escalation_decision.skip_quality_check = True

            logger.info(
                "Policy directive resolved: "
                f"action={policy_action}, reason={policy_reason}, mode={grounding_mode}"
            )

            logger.info("Generating AI response...")
            prompt_components = rag_service.assemble_prompt(
                ai_config=tenant_ai_config,
                user_query=message_data.message,
                retrieved_chunks=retrieved_chunks,
                conversation_history=conversation_history,
                company_name=tenant_response.display_name,
                company_description=tenant_response.description,
                max_history_turns=6,
                business_profile=business_profile,
            )
            directive_block = (
                "\n\nRuntime policy decision for this turn:\n"
                f"- Required action: {policy_action}\n"
                f"- Reason: {policy_reason}\n"
                f"- Retrieved chunks: {len(retrieved_chunks)}\n"
                f"- Average similarity: {avg_similarity:.3f}\n"
                "- Execution rules:\n"
                "  * action=answer: answer directly if safe and grounded.\n"
                "  * action=clarify: ask one concise clarifying question; do not fabricate facts.\n"
                "  * action=escalate: briefly explain and ask consent to connect to support.\n"
                "  * action=refuse: politely refuse and redirect to tenant support scope.\n"
                "- Always follow tenant custom instructions and brand voice.\n"
                "- Do not use emojis."
            )
            prompt_components.full_prompt = f"{prompt_components.full_prompt}{directive_block}"

            if _sales_mode:
                ai_response_text, _sales_meta, token_count, is_api_error = (
                    await rag_service.generate_sales_response(
                        prompt=prompt_components,
                        config=sales_agent_config,
                        model_name=settings.OPENAI_LLM_MODEL,
                    )
                )
            else:
                _sales_meta = None
                ai_response_text, token_count, is_api_error = (
                    await rag_service.generate_ai_response(
                        prompt=prompt_components,
                        max_tokens=tenant_ai_config.max_response_tokens,
                        model_name=settings.OPENAI_LLM_MODEL,
                    )
                )

            if token_count and prompt_components.prompt_tokens:
                completion_tokens = token_count - prompt_components.prompt_tokens
                logger.info(
                    f"Token usage - "
                    f"prompt={prompt_components.prompt_tokens}, "
                    f"completion={completion_tokens}, "
                    f"total={token_count}"
                )
            else:
                logger.info(f"Generated response ({token_count} tokens)")

            if is_api_error:
                logger.error("API error occurred, returning error message without escalation")
                quality = ResponseQuality(
                    is_substantial=False, has_uncertainty=True, word_count=0, char_count=0
                )
            else:
                if not escalation_decision.skip_quality_check:
                    quality = rag_service.evaluate_response_quality(ai_response_text)
                    logger.info(
                        f"Response quality: {quality.word_count} words, "
                        f"substantial={quality.is_substantial}, "
                        f"uncertain={quality.has_uncertainty}"
                    )

                    if not quality.is_substantial:
                        logger.info("Response quality insufficient, offering escalation")
                        escalation_decision.action = EscalationAction.OFFER_ESCALATION
                        escalation_decision.reason = "AI response quality below threshold"
                        ai_response_text = f"{ai_response_text}\n\nWould you like me to connect you with someone from our team?"
                else:
                    quality = ResponseQuality(
                        is_substantial=True,
                        has_uncertainty=False,
                        word_count=len(ai_response_text.split()),
                        char_count=len(ai_response_text),
                    )

        # Keep ticket summary context available after generation path.
        conversation_history = conversation_service.get_session_messages(
            session_id=session_id,
            limit=20,
        )

        # Sales lead orchestration — SALES MODE only.
        # Runs after ai_response_text is finalized so it never blocks the reply.
        lead: Lead | None = None  # resolved below; reused by quote orchestration
        _lead_captured_meta: dict | None = None
        if _sales_mode and _sales_meta is not None:
            try:
                lead_svc = LeadService(db)
                conv_id = session_id  # conversation_id == session_id in this handler

                # Determine if a lead already exists for this conversation.
                _existing = db.execute(
                    select(Lead).where(
                        Lead.tenant_id == session.tenant_id,
                        Lead.conversation_id == conv_id,
                    )
                ).scalar_one_or_none()

                _should_create = (
                    _existing is not None
                    or is_trigger_intent(
                        _sales_meta["intent"],
                        sales_agent_config.trigger_intents,
                    )
                )

                if _should_create:
                    # Resolve UTM from visitor session metadata.
                    _utm: dict | None = None
                    if isinstance(message_data.metadata, dict):
                        _utm = {
                            k: message_data.metadata[k]
                            for k in ("utm_source", "utm_medium", "utm_campaign")
                            if k in message_data.metadata
                        } or None

                    lead = lead_svc.get_or_create_by_conversation(
                        session.tenant_id,
                        conv_id,
                        intent=_sales_meta["intent"],
                        source="chat_widget",
                        utm=_utm,
                    )

                    _email_before = lead.email  # capture before enrich

                    lead = lead_svc.enrich_from_chat(
                        lead,
                        intent=_sales_meta["intent"],
                        captured=_sales_meta["captured"],
                        signals=_sales_meta["signals"],
                    )

                    # Set lead_captured only on the turn the email first appears.
                    if not _email_before and lead.email:
                        _lead_captured_meta = {
                            "name": lead.name,
                            "email": lead.email,
                        }
            except Exception as _lead_exc:
                logger.error(f"Sales lead orchestration error (non-fatal): {_lead_exc}")

        # Step 8: Save assistant message
        # Store as strings to keep JSON serialization safe (JSON column doesn't
        # serialize UUIDs natively).
        retrieved_chunk_ids: list[str] = []
        for chunk in retrieved_chunks:
            try:
                retrieved_chunk_ids.append(str(chunk.id))
            except Exception:
                continue
        avg_similarity = (
            (sum(similarity_scores) / len(similarity_scores)) if similarity_scores else 0.0
        )
        retrieval_hit = (
            len(retrieved_chunks) > 0 and avg_similarity >= rag_service.MIN_RELEVANT_SIMILARITY
        )
        _message_meta: dict = {
            "chunks_used": len(retrieved_chunks),
            "avg_similarity": round(avg_similarity, 3),
            "retrieval_hit": retrieval_hit,
            "escalation_action": escalation_decision.action.value,
            "response_quality": {
                "is_substantial": quality.is_substantial,
                "has_uncertainty": quality.has_uncertainty,
                "word_count": quality.word_count,
                "char_count": quality.char_count,
            },
            "token_breakdown": {
                "prompt_tokens": (
                    prompt_components.prompt_tokens
                    if prompt_components is not None
                    and prompt_components.prompt_tokens is not None
                    else 0
                ),
                "completion_tokens": (
                    token_count - prompt_components.prompt_tokens
                    if token_count
                    and prompt_components is not None
                    and prompt_components.prompt_tokens
                    else 0
                ),
                "total_tokens": token_count,
            },
        }
        if _lead_captured_meta is not None:
            _message_meta["lead_captured"] = _lead_captured_meta
        if _sales_mode and _sales_meta is not None:
            _signals = _sales_meta.get("signals") or {}
            if bool(_signals.get("offer_meeting")) and getattr(sales_agent_config, "meeting_booking_enabled", False):
                _message_meta["offer_meeting"] = True
            if bool(_signals.get("offer_quote")) and getattr(sales_agent_config, "quotation_enabled", False):
                try:
                    _ts_raw = (
                        (lead.team_size if lead is not None else None)
                        or (_sales_meta.get("captured") or {}).get("team_size")
                    )
                    _team_size: int | None = None
                    if _ts_raw is not None:
                        _m = re.search(r"\d+", str(_ts_raw))
                        _team_size = int(_m.group()) if _m else None
                    if _team_size is not None:
                        _q = compute_quote(
                            sales_agent_config.pricing_tiers or [],
                            getattr(sales_agent_config, "annual_discount_pct", 0),
                            sales_agent_config.currency,
                            team_size=_team_size,
                            plan_interest=(_sales_meta.get("captured") or {}).get("plan_interest"),
                        )
                        if _q is not None and lead is not None:
                            QuoteService(db).create_quote(
                                session.tenant_id,
                                lead,
                                currency=_q["currency"],
                                line_items=_q["line_items"],
                                total=_q["total"],
                            )
                            _message_meta["quote"] = _q
                except Exception as _quote_exc:
                    logger.error(f"Quote orchestration error (non-fatal): {_quote_exc}")

        assistant_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=ai_response_text,
            retrieved_chunk_ids=retrieved_chunk_ids,
            token_count=token_count,
            meta=_message_meta,
        )
        logger.info(f"Saved assistant message: {assistant_message.id}")

        # Track metrics for monitoring
        if similarity_scores:
            rag_service.track_retrieval_metrics(
                query=message_data.message,
                retrieved_chunks_count=len(retrieved_chunks),
                avg_similarity=avg_similarity,
                response_quality=quality,
                escalated=(escalation_decision.action != EscalationAction.NO_ESCALATION),
                generation_skipped=should_skip_generation,
                skip_reason=skip_reason,
                prompt_tokens=(
                    prompt_components.prompt_tokens
                    if "prompt_components" in locals() and prompt_components
                    else None
                ),
            )

        # Step 9: Handle escalation based on action
        ticket_id = None
        should_escalate = False
        escalation_reason: str | None = None

        if escalation_decision.action == EscalationAction.OFFER_ESCALATION:
            # Store pending escalation in session meta (wait for user consent)
            session_meta = session.meta or {}
            session_meta["pending_escalation"] = True
            session_meta["escalation_context"] = {
                "reason": escalation_decision.reason,
                "original_query": message_data.message,
            }
            conversation_service.update_session_meta(session_id, session_meta)
            logger.info("Pending escalation stored, waiting for user consent")
        elif pending_contact_capture_reason:
            session_meta = dict(session.meta or {})
            _store_pending_contact_capture(
                conversation_service=conversation_service,
                session_id=session_id,
                session_meta=session_meta,
                escalation_reason=pending_contact_capture_reason,
                customer_query=pending_contact_capture_query or message_data.message,
                required_fields=pending_contact_capture_fields,
            )
            logger.info("Stored pending contact capture for deferred escalation")

        elif escalation_decision.action == EscalationAction.AUTO_ESCALATE:
            # User explicitly requested human or critical keyword detected - escalate immediately
            session_meta = dict(session.meta or {})
            contact_name = _resolve_customer_name(
                session_meta,
                message_data.metadata,
                linked_visitor.name if linked_visitor else None,
            )
            contact_email = _resolve_customer_contact_email(
                session_meta,
                message_data.metadata,
                linked_visitor.email if linked_visitor else None,
            )
            if contact_name and session_meta.get("contact_name") != contact_name:
                session_meta["contact_name"] = contact_name
                conversation_service.update_session_meta(session_id, session_meta)
                _persist_contact_name_to_visitor(visitor_service, session_id, contact_name)
            if contact_email and session_meta.get("contact_email") != contact_email:
                session_meta["contact_email"] = contact_email
                conversation_service.update_session_meta(session_id, session_meta)
                _persist_contact_email_to_visitor(visitor_service, session_id, contact_email)

            should_escalate = True
            logger.info("Auto-escalating: Creating ticket...")
            escalation_reason = escalation_decision.reason or "Escalation requested"
            conversation_service.escalate_session(
                session_id=session_id,
                reason=escalation_reason,
            )

            # Create support ticket
            try:

                ticket_service = TicketService(db)

                # Generate smart summary from conversation
                summary = _generate_ticket_summary(
                    conversation_history=conversation_history,
                    escalation_reason=escalation_reason,
                    user_query=message_data.message,
                )

                # Determine priority based on keywords and escalation reason
                priority = _determine_ticket_priority(
                    user_query=message_data.message,
                    escalation_reason=escalation_reason,
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
                tenant_for_email = tenant_service.get_tenant_by_id(
                    session.tenant_id, include_relations=False
                )
                tenant_name = tenant_for_email.display_name if tenant_for_email else "Unknown"

                # Get all tenant admins and support agents

                notification_recipients = (
                    db.query(User)
                    .filter(
                        User.tenant_id == session.tenant_id,
                        User.is_active.is_(True),
                        User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                    )
                    .all()
                )

                # Send email to each recipient
                for recipient in notification_recipients:
                    try:
                        email_sent = await send_ticket_escalation_email(
                            email_to=recipient.email,
                            user_name=recipient.full_name,
                            tenant_name=tenant_name,
                            ticket_number=ticket.ticket_number,
                            escalation_reason=escalation_reason,
                            customer_query=message_data.message,
                            ticket_priority=priority.value.upper(),
                            ticket_id=str(ticket.id),
                            customer_contact_email=contact_email,
                        )
                        if email_sent:
                            logger.info(
                                f"✓ Escalation email sent successfully to {recipient.email}"
                            )
                        else:
                            logger.error(
                                f"✗ Failed to send escalation email to {recipient.email} (check email configuration and logs above)"
                            )
                    except Exception as email_error:
                        # Log but don't fail ticket creation if email fails
                        logger.error(
                            f"✗ Exception sending escalation email to {recipient.email}: {email_error}"
                        )

            except Exception as ticket_error:
                logger.error(f"Failed to create ticket or send notifications: {ticket_error}")
                # Don't fail the request if ticket creation fails

        # Step 10: Return response
        return ChatResponse(
            session_id=session_id,
            message=assistant_message,
            should_escalate=should_escalate,
            escalation_reason=escalation_reason if should_escalate else None,
            ticket_id=ticket_id,
        )

    except Exception as e:
        logger.error(f"Error processing chat message: {e}")

        # Save error message for user
        error_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content="I apologize, but I'm having trouble processing your request. Please try again or contact support.",
            meta={"error": str(e)},
        )

        return ChatResponse(
            session_id=session_id,
            message=error_message,
            should_escalate=True,
            escalation_reason="System error during processing",
            ticket_id=None,
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
    status_filter: SessionStatus | None = Query(None, description="Filter by status"),
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
    limit: int | None = Query(None, ge=1, le=500, description="Limit number of messages"),
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
    _subscription_check: object = Depends(verify_subscription_active),
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

    ticket = (
        db.query(Ticket)
        .filter(Ticket.session_id == session_id)
        .order_by(Ticket.created_at.desc())
        .first()
    )

    if ticket:

        ticket_service = TicketService(db)
        try:
            ticket_service.assign_ticket(
                ticket_id=ticket.id, assign_data=TicketAssign(assigned_to=current_user.id)
            )
            logger.info("Assigned ticket to agent after takeover")
        except ValueError as e:
            logger.warning(f"Could not assign ticket: {e}")

    # Send system message to indicate agent joined
    conversation_service.add_message(
        session_id=session_id,
        role=MessageRole.SYSTEM,
        content=f"{current_user.full_name} joined the conversation",
        meta={"event": "agent_joined", "agent_id": str(current_user.id)},
    )

    # Broadcast to customer via WebSocket (local + cross-worker)
    await connection_manager.fanout_event(
        session_id=session_id,
        message={
            "type": "agent_joined",
            "agent_name": current_user.full_name,
            "agent_id": str(current_user.id),
            "timestamp": (
                updated_session.agent_joined_at.isoformat()
                if updated_session.agent_joined_at
                else None
            ),
        },
        to_customer=True,
        to_agents=False,
    )

    logger.info("Agent took over session")

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
    _subscription_check: object = Depends(verify_subscription_active),
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
        # DB enum currently stores lower-case human_agent while user/assistant/system
        # are upper-case. Send explicit value to avoid enum-name mismatch.
        role=MessageRole.HUMAN_AGENT.value,  # type: ignore[arg-type]
        content=message_data.message,
        meta={
            "agent_id": str(current_user.id),
            "agent_name": current_user.full_name,
        },
    )

    # Broadcast to customer + other agents (local + cross-worker)
    await connection_manager.fanout_event(
        session_id=session_id,
        message={
            "type": "agent_message",
            "message_id": str(message.id),
            "content": message.content,
            "agent_name": current_user.full_name,
            "timestamp": message.created_at.isoformat(),
        },
        to_customer=True,
        to_agents=True,
        exclude_agent_id=current_user.id,
    )

    logger.info("Agent sent message in session")

    return message
