"""Chat and conversation endpoints."""

import asyncio
import io
import re
from typing import Any, cast
from types import SimpleNamespace
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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
    AttachmentDirection,
    AttachmentKind,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    EscalationAction,
    MessageRole,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.business_profile import BusinessProfile
from app.db.models.flow import Flow, FlowExecution, FlowStatus
from app.db.models.tenant import Tenant, TenantAIConfig
from app.db.models.user import User, UserRole
from app.db.models.visitor import VisitorSource
from app.schemas.conversation import (
    AttachmentOut,
    ChatResponse,
    ConversationSessionCreate,
    ConversationSessionUpdate,
    MessageRequest,
    MessageResponse,
    SessionCreateRequest,
    SessionResponse,
    StaffSessionDetailResponse,
    StaffSessionResponse,
)
from app.schemas.rag import EscalationDecision, ResponseQuality
from app.schemas.ticket import TicketCreate, TicketResponse
from app.schemas.tenant import TenantResponse
from app.schemas.user import UserResponse
from app.schemas.visitor import VisitorEnrichmentUpdate
from app.db.models.lead import Lead
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.offerings.cart import CartResolution, InterestSync, resolve_cart
from app.offerings.fallback import ASK_TEAM
from app.offerings.guard_flow import guard_reply
from app.offerings.misses import record_miss
from app.offerings.orchestrate import (
    alert_owner,
    catalog_score_signals,
    issue_quote,
    record_cart,
)
from app.offerings.turn import (
    customer_texts,
    ground_turn,
    kb_text_from_prompt,
    lead_source,
    build_offerings_turn,
    remember_shown,
    tier_amounts,
    turn_channel,
)
from app.offerings.turn import sales_kwargs as catalog_sales_kwargs
from app.sales.config_view import SalesConfigView
from app.sales.intent import is_trigger_intent
from app.schemas.agent_app import AcknowledgeLost
from app.services.conversation_counting import (
    WELCOME_TRIGGERS,
    enforce_conversation_limit,
    is_stale_closed,
    reopen_if_closed,
)
from app.services.conversation_service import (
    ConversationService,
    attachment_frames,
    emit_agent_joined,
    new_session_attribution,
)
from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator
from app.services.entitlements import tenant_has
from app.services.screen_recording.gating import screen_recording_allowed
from app.services.first_responder import (
    FIRST_RESPONDER_HUMAN,
    HUMAN_FIRST_REASON,
    HUMAN_FIRST_REPLY,
    HUMAN_FIRST_WELCOME,
    SCREEN_RECORDING_ACK,
    SCREEN_RECORDING_REASON,
)
from app.services.flow_engine import FlowEngine
from app.sales.pricing import compute_quote
from app.services.lead_service import LeadService
from app.services.notification_enqueue import (
    enqueue_agent_alerts_async,
    enqueue_clear_alert_async,
)
from app.services.quote_service import QuoteService
from app.services.rag_service import RAGService
from app.services.rating_read_service import session_rating_briefs
from app.services.reply_to import (
    ReplySnapshot,
    frame_reply_to,
    quote_context,
    resolve_for_session_id,
)
from app.services.tenant_service import TenantService
from app.services.voice import turn_signal
from app.services.ticket_service import TicketService
from app.services.visitor_service import VisitorService
from app.utils.datetime import ensure_utc_aware, utcnow_aware, utcnow_naive
from app.utils.email import send_ticket_escalation_email
from app.services.image_moderation import moderate_image
from app.utils.cloudinary_client import cloudinary_manager
from app.utils.http_rate_limiter import chat_rate_limit, chat_upload_rate_limit
from starlette.concurrency import run_in_threadpool

from app.services.customer_presence import read_presence, read_presence_many
from app.utils.customer_credential import (
    invalid_credential,
    optional_customer,
    session_credential_matches,
)
from app.utils.staff_redaction import redact_staff_messages, staff_customer_label

router = APIRouter()


def require_customer_credential(
    db: Session,
    request: Request,
    session: ConversationSession,
    *,
    user_identifier: str | None = None,
    visitor_token: str | None = None,
) -> None:
    """Customer credential check for the media paths (upload, video sign/register).

    A supplied (explicit) credential must match. A request with none is let through only while
    `CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL` is off, because live clients still send just
    the session id on these paths.
    """
    # Only an explicit credential counts as "supplied": a signed-in customer's cookie rides
    # along on every call (credentials: 'include'), including on someone else's guest page.
    explicit = bool(user_identifier or visitor_token)
    if not explicit and not settings.CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL:
        return
    customer = optional_customer(request, db)
    if not session_credential_matches(
        db,
        session,
        user_identifier=user_identifier,
        visitor_token=visitor_token,
        customer=customer,
    ):
        raise invalid_credential()


_CHAT_UPLOAD_MAX_MB = 10
_CHAT_UPLOAD_MAX_BYTES = _CHAT_UPLOAD_MAX_MB * 1024 * 1024


async def _process_image_upload(
    file: UploadFile,
    session: ConversationSession,
    direction: AttachmentDirection,
    db: Session,
) -> MessageAttachment:
    """Shared upload pipeline: bounded read, validate, moderate (fail-closed), store, insert.

    Authorization/feature gates are the caller's job. Raises HTTPException
    (413 too large, 415 invalid image, 422 image_rejected, 502 storage failure).
    """
    # Bounded read: never buffer more than cap+1 bytes (covers chunked bodies).
    content = await file.read(_CHAT_UPLOAD_MAX_BYTES + 1)
    return await process_image_bytes(
        content,
        file.content_type or "application/octet-stream",
        file.filename or "image",
        session,
        direction,
        db,
    )


async def screen_image_bytes(content: bytes, content_type: str) -> None:
    """Size cap, real-format check and fail-closed moderation: everything that needs no session.

    Nothing is stored. Raises HTTPException (413 too large, 415 invalid image, 422 image_rejected).
    """
    if len(content) > _CHAT_UPLOAD_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image too large")

    is_valid, error = cloudinary_manager.validate_image(content, max_size_mb=_CHAT_UPLOAD_MAX_MB)
    if not is_valid:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=error or "Invalid image",
        )

    # Moderation precedes storage: a flagged image is never uploaded. Fail closed.
    try:
        allowed, reason = await moderate_image(content, content_type)
    except Exception as e:
        logger.error(f"Chat image moderation raised (fail-closed): {e}")
        allowed, reason = False, "Image could not be verified"
    if not allowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "image_rejected", "reason": reason},
        )


async def process_image_bytes(
    content: bytes,
    content_type: str,
    filename: str,
    session: ConversationSession,
    direction: AttachmentDirection,
    db: Session,
) -> MessageAttachment:
    """Bytes-based core of the image pipeline: screen, then store (see the two halves).

    Raises HTTPException (413 too large, 415 invalid image, 422 image_rejected, 502 storage).
    """
    await screen_image_bytes(content, content_type)
    return store_screened_image(content, content_type, filename, session, direction, db)


def store_screened_image(
    content: bytes,
    content_type: str,
    filename: str,
    session: ConversationSession,
    direction: AttachmentDirection,
    db: Session,
) -> MessageAttachment:
    """Upload bytes that `screen_image_bytes` already passed and insert the attachment row
    (30-day retention comes from the model default). Raises HTTPException 502 on storage failure.

    Callers MUST screen first: this function does not moderate.
    """
    url, public_id, upload_error = cloudinary_manager.upload_chat_image(
        file_content=content,
        tenant_id=session.tenant_id,
        session_id=session.id,
        filename=filename,
    )
    if upload_error or not url or not public_id:
        logger.error(f"Chat image upload failed: {upload_error}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Image upload failed")

    width = height = None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as img:
            width, height = img.width, img.height
    except Exception:
        pass

    attachment = MessageAttachment(
        session_id=session.id,
        tenant_id=session.tenant_id,
        direction=direction,
        status=AttachmentStatus.UPLOADED,
        moderation_status=AttachmentModeration.PASSED,
        cloudinary_public_id=public_id,
        url=url,
        content_type=content_type,
        width=width,
        height=height,
        bytes=len(content),
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)

    return attachment


def _upload_response(attachment: MessageAttachment) -> dict:
    return {
        "attachment_id": str(attachment.id),
        "url": attachment.url,
        "content_type": attachment.content_type,
        "width": attachment.width,
        "height": attachment.height,
        "kind": getattr(attachment.kind, "value", attachment.kind),
        "duration_ms": attachment.duration_ms,
        "poster_url": attachment.poster_url,
        "bytes": attachment.bytes,
    }


@router.post(
    "/upload",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(chat_upload_rate_limit)],
)
async def upload_chat_image(
    request: Request,
    session_id: UUID = Form(...),
    file: UploadFile = File(..., description="Image (JPEG, PNG, GIF, WebP), max 10MB"),
    user_identifier: str | None = Form(None, max_length=255),
    visitor_token: str | None = Form(None, max_length=255),
    db: Session = Depends(get_db),
):
    """Public customer image upload: validate, moderate, then store (unlinked)."""
    session = db.execute(
        select(ConversationSession).where(ConversationSession.id == session_id)
    ).scalar_one_or_none()
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    require_customer_credential(
        db, request, session, user_identifier=user_identifier, visitor_token=visitor_token
    )

    tenant = db.execute(select(Tenant).where(Tenant.id == session.tenant_id)).scalar_one_or_none()
    if not tenant or not tenant.widget_allow_file_upload:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="File upload is not enabled")

    attachment = await _process_image_upload(file, session, AttachmentDirection.INBOUND, db)
    return _upload_response(attachment)


IMAGE_ONLY_PLACEHOLDER = "[image]"
VIDEO_ONLY_PLACEHOLDER = "[screen recording]"
SYSTEM_START_TRIGGER = "start"
FLOW_START_PREFIX = "__flow_start__:"
TALK_TO_HUMAN_TOKEN = "__talk_to_human__"
# Readable text stored/used in place of the control token when the router is skipped (human-only mode).
TALK_TO_HUMAN_TEXT = "I'd like to talk to a person"
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
    if entry_point not in {
        VisitorSource.WIDGET.value,
        VisitorSource.SUPPORT_PAGE.value,
        VisitorSource.WHATSAPP.value,
    }:
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


def _without_customer_user_id(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Client metadata minus `customer_user_id`: that binding comes only from an
    authenticated customer token, never from a caller-supplied field."""
    return {k: v for k, v in (metadata or {}).items() if k != "customer_user_id"}


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


async def _ring_agents(ticket: TicketResponse | None) -> None:
    """Ring the tenant's agents on their phones for a fresh escalation.

    Best-effort by design: the email escalation is the always-on backstop, so a
    Redis hiccup must never turn into a failed customer chat response.

    `attempt` is derived from `ticket.updated_at` so a freshly created ticket
    and a MISSED ticket just reopened for re-escalation (see
    `_get_or_create_escalation_ticket`) get distinct ARQ job ids — reusing the
    same id for a later, distinct ring episode would let ARQ's own dedup
    silently swallow it (job keys live ~24h regardless of completion).
    """
    if ticket is None or not settings.AGENT_ALERT_ENABLED:
        return
    attempt = int(ticket.updated_at.timestamp() * 1000)
    try:
        await enqueue_agent_alerts_async(
            ticket.id, settings.AGENT_ALERT_WAIT_SECONDS, attempt=attempt
        )
    except Exception:  # noqa: BLE001 — never break the chat reply over a push
        logger.exception("Failed to enqueue agent alert for ticket %s", ticket.id)


# Strong refs so fire-and-forget notification tasks are not garbage-collected mid-send.
_background_notifications: set[asyncio.Task] = set()


async def _send_escalation_emails(
    *,
    recipients: list[tuple[str, str]],
    tenant_name: str,
    ticket_number: str,
    ticket_id: str,
    escalation_reason: str,
    customer_query: str,
    priority_label: str,
    contact_email: str | None,
) -> None:
    """Deliver the escalation email to each recipient. Never raises: one bad
    address must not stop the rest, and nobody is waiting on this to answer."""
    for email_to, user_name in recipients:
        try:
            sent = await send_ticket_escalation_email(
                email_to=email_to,
                user_name=user_name,
                tenant_name=tenant_name,
                ticket_number=ticket_number,
                escalation_reason=escalation_reason,
                customer_query=customer_query,
                ticket_priority=priority_label,
                ticket_id=ticket_id,
                customer_contact_email=contact_email,
            )
            if not sent:
                logger.error(f"Escalation email to {_mask_email(email_to)} was not sent")
        except Exception as email_error:  # noqa: BLE001 -- log, keep going
            logger.exception(f"Escalation email to {_mask_email(email_to)} failed: {email_error}")


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _schedule_escalation_emails(**kwargs: Any) -> None:
    """Run `_send_escalation_emails` off the request path (each mail is ~2-3 s
    of SMTP; three of them used to stall the escalating turn for ~8 s, which
    on voice made the tool call arrive after the buffer word). Never raises."""
    try:
        task = asyncio.create_task(_send_escalation_emails(**kwargs))
    except Exception as exc:  # noqa: BLE001 -- notification must never fail the turn
        logger.exception(f"Could not schedule escalation emails: {exc}")
        return
    _background_notifications.add(task)
    task.add_done_callback(_background_notifications.discard)


def _queue_escalation_emails(
    db: Session,
    tenant_service: TenantService,
    *,
    tenant_id: UUID,
    ticket: Any,
    escalation_reason: str,
    customer_query: str,
    priority_label: str,
    contact_email: str | None,
) -> None:
    """Resolve the recipients (two cheap reads) and hand delivery to a background
    task. Same recipients and content as the former inline loop."""
    try:
        tenant = tenant_service.get_tenant_by_id(tenant_id, include_relations=False)
        recipients = [
            (u.email, u.full_name)
            for u in db.query(User)
            .filter(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
            )
            .all()
        ]
        _schedule_escalation_emails(
            recipients=recipients,
            tenant_name=tenant.display_name if tenant else "Unknown",
            ticket_number=ticket.ticket_number,
            ticket_id=str(ticket.id),
            escalation_reason=escalation_reason,
            customer_query=customer_query,
            priority_label=priority_label,
            contact_email=contact_email,
        )
    except Exception as exc:  # noqa: BLE001 -- the ticket already exists; do not fail the turn
        logger.exception(f"Failed to queue escalation emails: {exc}")


def _get_or_create_escalation_ticket(
    ticket_service: TicketService,
    session_id: UUID,
    ticket_data: TicketCreate,
) -> tuple[TicketResponse, bool]:
    """Return the session's live escalation ticket, creating one if none exists.

    A session can be re-escalated while its first ticket is still open — the
    visitor sends another message, a flow re-matches, the router fires again.
    `escalate_session` already no-ops the session fields on that path; this is
    the ticket-side half of the same guard. Reusing the live ticket instead of
    minting a second one keeps the ring to exactly one per live escalation.

    A MISSED, still-unassigned ticket is a special case, not an ordinary
    reuse: tier 1 already failed to answer once, and `auto_resolve_stale_tickets`
    never sweeps MISSED tickets (it only ages out OPEN/IN_PROGRESS) — so
    nothing else will ever ring this session again unless re-escalating it
    does. Reopen it back to OPEN, reset its ring bookkeeping so `claim_log`
    doesn't skip agents already (unsuccessfully) rung the first time, and ring
    again. The customer who already waited longest and was failed once must
    not be the one silently dropped forever.

    Returns:
        (ticket, should_ring) — True for a brand-new ticket and for a
        just-reopened MISSED one; False for any other reuse (already OPEN or
        IN_PROGRESS, and therefore already ringing or already being handled).
    """
    # Lock + re-check + reuse/reopen/create live in TicketService so chat, voice and
    # manual POST /tickets share one implementation.
    return ticket_service.get_or_create_live_ticket(ticket_data)


async def _escalate_to_human(
    db: Session,
    *,
    conversation_service,
    tenant_service,
    session,
    session_id: UUID,
    escalation_reason: str,
    customer_query: str,
    conversation_history: list,
    contact_email: str | None,
) -> UUID | None:
    """escalate_session -> get-or-create ticket -> ring (new/reopened) -> queue emails.

    Returns the ticket id, or None if ticket creation failed (logged, never raised).
    """
    conversation_service.escalate_session(
        session_id=session_id,
        reason=escalation_reason,
    )

    ticket_id = None
    # Create support ticket
    try:

        ticket_service = TicketService(db)

        # Generate smart summary from conversation
        summary = _generate_ticket_summary(
            conversation_history=conversation_history,
            escalation_reason=escalation_reason,
            user_query=customer_query,
        )

        # Determine priority based on keywords and escalation reason
        priority = _determine_ticket_priority(
            user_query=customer_query,
            escalation_reason=escalation_reason,
        )

        # Create ticket
        ticket_data = TicketCreate(
            tenant_id=session.tenant_id,
            session_id=session_id,
            priority=priority,
            summary=summary,
        )
        ticket, ticket_created = _get_or_create_escalation_ticket(
            ticket_service, session_id, ticket_data
        )
        ticket_id = ticket.id
        if ticket_created:
            await _ring_agents(ticket)

            logger.info(f"Ticket created: {ticket.ticket_number} with {priority} priority")

            # Send email notifications to tenant admins and support agents
            _queue_escalation_emails(
                db,
                tenant_service,
                tenant_id=session.tenant_id,
                ticket=ticket,
                escalation_reason=escalation_reason,
                customer_query=customer_query,
                priority_label=priority.value.upper(),
                contact_email=contact_email,
            )

    except Exception as ticket_error:
        logger.error(f"Failed to create ticket or send notifications: {ticket_error}")
        # Don't fail the request if ticket creation fails
    return ticket_id


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

    # A new session is a new counted conversation: same cap, same 402 as the message route.
    enforce_conversation_limit(db, session_data.tenant_id)

    # Convert SessionCreateRequest to ConversationSessionCreate
    visitor_id, visitor_session_id = _resolve_bootstrapped_visitor_context(
        visitor_service,
        session_data.tenant_id,
        session_data.metadata,
    )
    session_create = ConversationSessionCreate(
        tenant_id=session_data.tenant_id,
        user_identifier=session_data.user_identifier,
        visitor_id=visitor_id,
        visitor_session_id=visitor_session_id,
        meta=_without_customer_user_id(session_data.metadata),
    )

    session = conversation_service.create_session(session_create)

    return session


@router.post(
    "/message",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(chat_rate_limit)],
)
async def post_chat_message(
    message_data: MessageRequest,
    db: Session = Depends(get_db),
):
    """Public customer message. Posting into an existing session needs its credential."""
    if message_data.session_id:
        session = db.get(ConversationSession, message_data.session_id)
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
        visitor_token = (message_data.metadata or {}).get("visitor_token")
        if not session_credential_matches(
            db,
            session,
            user_identifier=message_data.user_identifier,
            visitor_token=visitor_token if isinstance(visitor_token, str) else None,
        ):
            raise invalid_credential()
    message_data.metadata = _without_customer_user_id(message_data.metadata) or None
    return await send_message(message_data, db)


async def send_message(
    message_data: MessageRequest,
    db: Session,
    *,
    authed_customer_id: UUID | None = None,
):
    """Send a customer message; surfaces any linked image attachments on the response.

    Trusted entry point: the caller has already authenticated the customer (the public
    route above, the customer-token route, or a voice token). `authed_customer_id` is
    the only way a session gets bound to a customer account.
    """
    linked_attachments: list[AttachmentOut] = []
    response = await _send_message_impl(
        message_data, db, linked_attachments, authed_customer_id=authed_customer_id
    )
    if (
        message_data.attachment_ids
        and not linked_attachments
        and isinstance(response, ChatResponse)
    ):
        linked_attachments = _fallback_link_attachments(
            db, response.session_id, message_data.attachment_ids
        )
    if linked_attachments and isinstance(response, ChatResponse):
        response.user_message_attachments = linked_attachments
        await _fanout_customer_images(db, response.session_id, linked_attachments)
    return response


def _reply_to_frame(message: Message) -> dict | None:
    return frame_reply_to(MessageResponse.model_validate(message).reply_to)


async def _fanout_customer_images(
    db: Session, session_id: UUID, attachments: list[AttachmentOut]
) -> None:
    """Push the customer's image message to watching agents."""
    await _fanout_customer_message(
        db,
        session_id,
        select(Message)
        .join(MessageAttachment, MessageAttachment.message_id == Message.id)
        .where(MessageAttachment.id == attachments[0].id),
    )


async def _fanout_customer_message(db: Session, session_id: UUID, query) -> None:  # type: ignore[no-untyped-def]
    """Push one persisted customer message (selected by `query`) to watching agents.

    REST sends need this: the widget uses the socket once escalated, but the signed-in customer
    chat (and the widget while its socket is down) send over REST. Best-effort: the message is
    already persisted, so a fanout failure must not fail the send.
    """
    try:
        message = db.execute(query.options(selectinload(Message.attachments))).scalar_one_or_none()
        session = db.get(ConversationSession, session_id)
        if message is None or session is None:
            return
        label = staff_customer_label(db, session.id, session.visitor_id)
        await connection_manager.fanout_event(
            session_id=session_id,
            message={
                "type": "customer_message",
                "message_id": str(message.id),
                "content": message.content,
                "customer_label": label,
                "timestamp": message.created_at.isoformat(),
                "attachments": attachment_frames(message.attachments),
                "reply_to": _reply_to_frame(message),
            },
            to_customer=False,
            to_agents=True,
        )
    except Exception:
        logger.exception("Failed to fan out customer message to agents")


def _fallback_link_attachments(
    db: Session, session_id: UUID, attachment_ids: list[UUID]
) -> list[AttachmentOut]:
    """Guard: a turn branch never persisted a USER message, so attachments are still orphaned.

    Link them to the most recent USER message of the session so images are not silently lost.
    """
    logger.error(
        "Chat turn finished with attachment_ids but none were linked; "
        "falling back to latest user message"
    )
    last_user = db.execute(
        select(Message)
        .where(Message.session_id == session_id, Message.role == MessageRole.USER)
        .order_by(Message.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last_user is None:
        logger.error("No user message in session to link orphaned attachments to")
        return []
    service = ConversationService(db)
    try:
        rows = service.link_attachments(session_id, last_user.id, attachment_ids)
        db.commit()
    except ValueError:
        db.rollback()
        logger.error("Fallback attachment linking failed validation")
        return []
    return [AttachmentOut.model_validate(r) for r in rows]


async def _send_message_impl(
    message_data: MessageRequest,
    db: Session,
    linked_attachments: list[AttachmentOut],
    authed_customer_id: UUID | None = None,
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

    # N2 reply-to: validate before anything is persisted or a closed session is reopened.
    # The quote is queued on the service and attached to the first USER message persisted.
    reply_snapshot: ReplySnapshot | None = None
    if message_data.reply_to_message_id:
        reply_snapshot = resolve_for_session_id(
            db, message_data.session_id, message_data.reply_to_message_id
        )
        conversation_service.queue_reply_to(reply_snapshot)

    # Image attachments: an image-only send carries a minimal placeholder so every
    # downstream string-based step (routing, flows, prompt assembly) stays valid.
    if not (message_data.message and message_data.message.strip()):
        message_data.message = IMAGE_ONLY_PLACEHOLDER

    # Step 1: Get or create session
    session_id = message_data.session_id
    session: SessionResponse | None
    stale_origin: ConversationSession | None = None
    if session_id:
        # An abandoned/resolved session whose last message is outside the reuse window is a
        # returning customer, not a continuation: it starts (and counts as) a new conversation.
        candidate = db.get(ConversationSession, session_id)
        if candidate is not None and is_stale_closed(candidate, message_data.message):
            stale_origin = candidate
            session_id = None
            message_data.session_id = None
            message_data.tenant_id = candidate.tenant_id
            message_data.user_identifier = message_data.user_identifier or candidate.user_identifier
            logger.info("Session past the reuse window; starting a new conversation")
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
        customer_user_id = authed_customer_id

        # If this customer already has a thread inside the reuse window (open, or abandoned /
        # resolved with a recent last message), continue it instead of opening a parallel
        # conversation. Reusing never creates a counted row.
        reused_session: SessionResponse | None = None
        if customer_user_id:
            reused_session = conversation_service.get_latest_session_for_customer(
                tenant_id=message_data.tenant_id,
                customer_user_id=customer_user_id,
                reuse_window=True,
            )
        elif visitor_id:
            reused_session = conversation_service.get_latest_session_for_visitor(
                tenant_id=message_data.tenant_id,
                visitor_id=visitor_id,
                reuse_window=True,
            )
        elif stale_origin is not None:
            # Clients that do not adopt a changed session_id keep sending the old one; point
            # them at the conversation we already opened for them rather than counting again.
            # Match only the old session's own identifier (the credential the caller was checked
            # against), never a visitor the request did not prove or an identifier it supplied.
            reused_session = conversation_service.get_active_session_for_user(
                stale_origin.tenant_id, stale_origin.user_identifier
            )
            # An unauthenticated caller must never land in a customer-bound thread.
            if reused_session is not None:
                target = db.get(ConversationSession, reused_session.id)
                if target is None or target.customer_user_id is not None:
                    reused_session = None

        if reused_session:
            session_id = reused_session.id
            existing_session_model = db.execute(
                select(ConversationSession).where(ConversationSession.id == session_id)
            ).scalar_one_or_none()
            if existing_session_model:
                if visitor_session_id:
                    existing_session_model.visitor_session_id = visitor_session_id
                reopen_if_closed(existing_session_model, message_data.message)
                db.commit()
            session = conversation_service.get_session(session_id) or reused_session
            logger.info("Reusing existing visitor session")
        else:
            enforce_conversation_limit(db, message_data.tenant_id)

            # Convert to ConversationSessionCreate
            attribution = new_session_attribution.get()
            session_create = ConversationSessionCreate(
                tenant_id=message_data.tenant_id,
                user_identifier=message_data.user_identifier,
                visitor_id=visitor_id,
                visitor_session_id=visitor_session_id,
                customer_user_id=customer_user_id,
                meta=_without_customer_user_id(message_data.metadata),
                source=attribution[0] if attribution else None,
                source_meta=attribution[1] if attribution else None,
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
            if (message_data.message or "").lower().strip() not in WELCOME_TRIGGERS:
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
    human_first = getattr(tenant_ai_config, "first_responder", "ai") == FIRST_RESPONDER_HUMAN
    require_email_before_escalation = bool(
        getattr(tenant_ai_config, "require_email_before_escalation", False)
    )
    require_name_before_escalation = bool(
        getattr(tenant_ai_config, "require_name_before_escalation", False)
    )

    # Load sales agent config (may be None if tenant has not configured it).
    sales_agent_config = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == session.tenant_id)
    ).scalar_one_or_none()
    _sales_mode = bool(
        sales_agent_config is not None
        and getattr(sales_agent_config, "agent_enabled", False)
        and getattr(sales_agent_config, "lead_capture_enabled", False)
    )
    # Meeting booking and quotes are plan-gated; lead capture and scoring are not. A tenant whose
    # plan lacks the gate gets the same chat as one with the options switched off.
    _sales_close_allowed = _sales_mode and tenant_has(db, session.tenant_id, "sales_close")
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

    # Attachments: validate up-front (nothing persisted on failure), then link them to
    # the USER message at whichever branch persists it. The send endpoint has many
    # branches that each call add_message(role=USER), so wrap add_message once here.
    turn_has_video = False
    if message_data.attachment_ids:
        try:
            _linkable = conversation_service.get_linkable_attachments(
                session_id, message_data.attachment_ids
            )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or already-used attachment",
            ) from None
        _video_count = sum(1 for a in _linkable if a.kind == AttachmentKind.VIDEO)
        if _video_count > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "too_many_videos", "message": "One recording per message"},
            )
        turn_has_video = _video_count == 1
        if turn_has_video and not screen_recording_allowed(db, db.get(Tenant, session.tenant_id)):
            # Registered before a downgrade or while recording was switched off: same answer
            # as the sign/register endpoints give.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "screen_recording_disabled",
                    "message": "Screen recording is not enabled",
                },
            )
        _attachment_ids = message_data.attachment_ids
        _plain_add_message = conversation_service.add_message
        _linked = False

        def _add_message_linking_attachments(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal _linked
            msg = _plain_add_message(*args, **kwargs)
            role = kwargs.get("role", args[1] if len(args) > 1 else None)
            if role == MessageRole.USER and not _linked:
                try:
                    rows = conversation_service.link_attachments(
                        session_id, msg.id, _attachment_ids
                    )
                except ValueError:
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid or already-used attachment",
                    ) from None
                db.commit()
                _linked = True
                linked_attachments.extend(AttachmentOut.model_validate(r) for r in rows)
            return msg

        conversation_service.add_message = _add_message_linking_attachments  # type: ignore[method-assign]

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

    # Screen recording (spec SR-D4): a video is a request for a human. It never reaches the
    # AI, flows or automations. Same contact gate + _escalate_to_human as talk-to-human;
    # an already-escalated session only re-rings when its ticket is MISSED (inside the helper).
    if turn_has_video:
        video_text = (
            VIDEO_ONLY_PLACEHOLDER
            if message_data.message == IMAGE_ONLY_PLACEHOLDER
            else message_data.message
        )
        video_user_message = conversation_service.add_message(
            session_id=session_id, role=MessageRole.USER, content=video_text
        )
        already_escalated = session.status == SessionStatus.ESCALATED
        if not already_escalated:
            video_missing = _get_missing_contact_fields(
                require_name_before_escalation=require_name_before_escalation,
                require_email_before_escalation=require_email_before_escalation,
                customer_name=customer_name,
                customer_contact_email=customer_contact_email,
            )
            if video_missing:
                _store_pending_contact_capture(
                    conversation_service=conversation_service,
                    session_id=session_id,
                    session_meta=dict(session_model.meta or {}),
                    escalation_reason=SCREEN_RECORDING_REASON,
                    customer_query=video_text,
                    required_fields=video_missing,
                )
                video_prompt = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=_build_contact_capture_prompt(video_missing),
                    meta={
                        "contact_capture_required": True,
                        "contact_capture_fields": video_missing,
                    },
                )
                return ChatResponse(
                    session_id=session_id,
                    message=video_prompt,
                    should_escalate=False,
                    escalation_reason=None,
                    ticket_id=None,
                )
        video_ticket_id = await _escalate_to_human(
            db,
            conversation_service=conversation_service,
            tenant_service=tenant_service,
            session=session,
            session_id=session_id,
            escalation_reason=SCREEN_RECORDING_REASON,
            customer_query=video_text,
            conversation_history=conversation_service.get_session_messages(
                session_id=session_id, limit=20
            ),
            contact_email=customer_contact_email,
        )
        if already_escalated:
            return ChatResponse(
                session_id=session_id,
                message=video_user_message,
                should_escalate=False,
                escalation_reason=None,
                ticket_id=None,
            )
        video_ack = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=SCREEN_RECORDING_ACK,
            meta={"system_notice": "screen_recording_ack"},
        )
        return ChatResponse(
            session_id=session_id,
            message=video_ack,
            should_escalate=True,
            escalation_reason=SCREEN_RECORDING_REASON,
            ticket_id=video_ticket_id,
        )

    # Escalated session: a human agent owns this conversation, so the bot must not auto-reply.
    # Customers still send images (and text) over REST after escalation because images cannot go
    # over the agent WebSocket; none of that should trigger an AI reply, a flow, or a welcome
    # message that intrudes on the human<->customer thread. The sole exception is the explicit
    # "talk to human" control token, which must still reach the escalation branch below so an
    # again-requested or previously-missed escalation re-rings the agent (see the re-escalation
    # regressions in test_chat_flow_router). For every other input we persist the customer's
    # message (linking any attachments via the add_message wrapper installed above, so the
    # wrapper's commit + agent-fanout path still fires) and return with no generation. The
    # ChatResponse echoes the persisted USER message because the schema requires a MessageResponse
    # and there is deliberately no assistant reply this turn.
    if session.status == SessionStatus.ESCALATED and effective_message_lower != TALK_TO_HUMAN_TOKEN:
        logger.info("Skipping AI generation: session is escalated to a human agent")
        user_message = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.USER,
            content=(message_data.message or "").strip() or IMAGE_ONLY_PLACEHOLDER,
        )
        if not message_data.attachment_ids:
            # Messages with attachments are fanned out by send_message once they're linked.
            await _fanout_customer_message(
                db, session_id, select(Message).where(Message.id == user_message.id)
            )
        return ChatResponse(
            session_id=session_id,
            message=user_message,
            should_escalate=False,
            escalation_reason=None,
            ticket_id=None,
        )

    # Global start router: if no active execution and at least one active flow,
    # present flow choices instead of relying on overlapping keywords.
    forced_flow: Flow | None = None
    active_exec = flow_engine._get_active_execution(session_model.id)
    if not active_exec and not human_first:
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
            # not using _escalate_to_human: fixed summary/HIGH priority, no history fetch
            conversation_service.escalate_session(
                session_id=session_id, reason=router_escalation_reason
            )
            ticket_id = None
            try:

                ticket_service = TicketService(db)
                ticket, ticket_created = _get_or_create_escalation_ticket(
                    ticket_service,
                    session_id,
                    TicketCreate(
                        tenant_id=session.tenant_id,
                        session_id=session_id,
                        priority=TicketPriority.HIGH,
                        summary="User selected Talk to an Agent from flow start menu.",
                    ),
                )
                ticket_id = ticket.id
                if ticket_created:
                    await _ring_agents(ticket)

                    # Notify support/admin team via email
                    _queue_escalation_emails(
                        db,
                        tenant_service,
                        tenant_id=session.tenant_id,
                        ticket=ticket,
                        escalation_reason=router_escalation_reason,
                        customer_query=message_data.message,
                        priority_label=TicketPriority.HIGH.value.upper(),
                        contact_email=contact_email,
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

    if (rag_only_mode or human_first) and not forced_flow:
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
                # not using _escalate_to_human: escalate_session runs before the assistant message is added and the history (incl. that message) is fetched after it; also flow-specific logs
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
                    ticket, ticket_created = _get_or_create_escalation_ticket(
                        ticket_service, session_id, ticket_data
                    )
                    ticket_id = ticket.id
                    if ticket_created:
                        await _ring_agents(ticket)
                        logger.info(f"Ticket created from flow: {ticket.ticket_number}")

                        # Send email notifications
                        _queue_escalation_emails(
                            db,
                            tenant_service,
                            tenant_id=session.tenant_id,
                            ticket=ticket,
                            escalation_reason=flow_escalation_reason,
                            customer_query=message_data.message,
                            priority_label=priority.value.upper(),
                            contact_email=contact_email,
                        )

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
                    # not using _escalate_to_human: escalate_session runs before the assistant message is added and the history (incl. that message) is fetched after it; also flow-specific logs
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
                        ticket, ticket_created = _get_or_create_escalation_ticket(
                            ticket_service, session_id, ticket_data
                        )
                        ticket_id = ticket.id
                        if ticket_created:
                            await _ring_agents(ticket)
                            logger.info(f"Ticket created from flow: {ticket.ticket_number}")

                            # Send email notifications
                            _queue_escalation_emails(
                                db,
                                tenant_service,
                                tenant_id=session.tenant_id,
                                ticket=ticket,
                                escalation_reason=continued_escalation_reason,
                                customer_query=message_data.message,
                                priority_label=priority.value.upper(),
                                contact_email=contact_email,
                            )

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

        # A reply is never a bare greeting: skipping this branch stores the USER message
        # (with its quote) instead of silently dropping it.
        if message_lower in welcome_triggers and reply_snapshot is None:
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
                content=(
                    HUMAN_FIRST_WELCOME.format(company_name=company_name)
                    if human_first
                    else f"Hi! Welcome to {company_name}. I'm here to help! What can I assist you with today?"
                ),
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

        # not using _escalate_to_human: escalate_session runs before the assistant message and the history is fetched after it; separate summary query vs email contact
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
            ticket, ticket_created = _get_or_create_escalation_ticket(
                ticket_service, session_id, ticket_data
            )
            ticket_id = ticket.id
            if ticket_created:
                await _ring_agents(ticket)

                _queue_escalation_emails(
                    db,
                    tenant_service,
                    tenant_id=session.tenant_id,
                    ticket=ticket,
                    escalation_reason=capture_escalation_reason,
                    customer_query=customer_query,
                    priority_label=priority.value.upper(),
                    contact_email=final_contact_email,
                )
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
            # not using _escalate_to_human: escalate_session runs before the assistant message is added and the history (incl. that message) is fetched after it
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
                ticket, ticket_created = _get_or_create_escalation_ticket(
                    ticket_service, session_id, ticket_data
                )
                ticket_id = ticket.id
                if ticket_created:
                    await _ring_agents(ticket)
                    logger.info(f"Ticket created: {ticket.ticket_number}")

                    # Send email notifications
                    _queue_escalation_emails(
                        db,
                        tenant_service,
                        tenant_id=session.tenant_id,
                        ticket=ticket,
                        escalation_reason=consent_escalation_reason,
                        customer_query=consent_original_query,
                        priority_label=priority.value.upper(),
                        contact_email=contact_email,
                    )

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

    # Step 4: Add user message to database. In human-only mode the router (which normally
    # consumes the talk-to-human control token) is skipped, so store readable text instead.
    stored_user_text = (
        TALK_TO_HUMAN_TEXT
        if human_first and effective_message_lower == TALK_TO_HUMAN_TOKEN
        else message_data.message
    )
    user_message = conversation_service.add_message(
        session_id=session_id, role=MessageRole.USER, content=stored_user_text
    )
    logger.info(f"Added user message: {user_message.id}")

    # N3 human-only mode: the AI and tenant automations never answer. Capture contact details
    # first when the tenant requires them (completion path escalates with the stored reason),
    # otherwise escalate immediately with a fixed reply.
    if human_first:
        human_first_meta = dict(session_model.meta or {})
        human_missing_fields = _get_missing_contact_fields(
            require_name_before_escalation=require_name_before_escalation,
            require_email_before_escalation=require_email_before_escalation,
            customer_name=_resolve_customer_name(
                human_first_meta,
                message_data.metadata,
                linked_visitor.name if linked_visitor else None,
            ),
            customer_contact_email=_resolve_customer_contact_email(
                human_first_meta,
                message_data.metadata,
                linked_visitor.email if linked_visitor else None,
            ),
        )
        if human_missing_fields:
            _store_pending_contact_capture(
                conversation_service=conversation_service,
                session_id=session_id,
                session_meta=human_first_meta,
                escalation_reason=HUMAN_FIRST_REASON,
                customer_query=stored_user_text,
                required_fields=human_missing_fields,
            )
            capture_prompt = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=_build_contact_capture_prompt(human_missing_fields),
                meta={
                    "contact_capture_required": True,
                    "contact_capture_fields": human_missing_fields,
                },
            )
            return ChatResponse(
                session_id=session_id,
                message=capture_prompt,
                should_escalate=False,
                escalation_reason=None,
                ticket_id=None,
            )
        human_ticket_id = await _escalate_to_human(
            db,
            conversation_service=conversation_service,
            tenant_service=tenant_service,
            session=session,
            session_id=session_id,
            escalation_reason=HUMAN_FIRST_REASON,
            customer_query=stored_user_text,
            conversation_history=conversation_service.get_session_messages(
                session_id=session_id, limit=20
            ),
            contact_email=_resolve_customer_contact_email(
                human_first_meta,
                message_data.metadata,
                linked_visitor.email if linked_visitor else None,
            ),
        )
        human_reply = conversation_service.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=HUMAN_FIRST_REPLY,
            meta={"human_first": True},
        )
        return ChatResponse(
            session_id=session_id,
            message=human_reply,
            should_escalate=True,
            escalation_reason=HUMAN_FIRST_REASON,
            ticket_id=human_ticket_id,
        )

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
            # Offerings: read the catalog live before the grounding policy runs, because a
            # catalog hit is grounding. None for a tenant with nothing listed (nothing changes).
            _offerings = ground_turn(
                db,
                session.tenant_id,
                message_data.message,
                history=conversation_history,
                session_meta=dict(session_model.meta or {}),
            )
            _catalog_grounded = bool(_offerings and _offerings.grounded)
            if scope_allowed:
                low_confidence_action = str(
                    getattr(tenant_ai_config, "low_confidence_action", "clarify")
                )
                no_retrieval_action = str(
                    getattr(tenant_ai_config, "no_retrieval_action", "clarify")
                )

                has_no_retrieval = len(retrieved_chunks) == 0 and not _catalog_grounded
                has_low_confidence = (
                    len(retrieved_chunks) > 0
                    and avg_similarity < rag_service.MIN_RELEVANT_SIMILARITY
                    and not _catalog_grounded
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

            if _catalog_grounded:
                # A short, correct catalog answer ("The black sneaker is ₦12,000.") is not a weak
                # one; the price guard below is what vets it.
                escalation_decision.skip_quality_check = True

            turn_signal.generation_started()
            logger.info("Generating AI response...")
            prompt_components = rag_service.assemble_prompt(
                ai_config=tenant_ai_config,
                user_query=quote_context(message_data.message, reply_snapshot),
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
            _kb_text = kb_text_from_prompt(prompt_components.full_prompt)
            if _offerings is not None:
                prompt_components.full_prompt += f"\n\n{_offerings.block}"
                if prompt_components.prompt_tokens is not None:
                    prompt_components.prompt_tokens += _offerings.token_count

            # Current turn's linked image URLs (populated when the USER message was
            # persisted); None keeps the text-only path unchanged.
            _turn_image_urls = [
                a.url for a in linked_attachments if a.kind == "image" and a.url
            ] or None

            if _sales_mode:
                # State-threading: pin facts already captured this conversation so
                # the agent never re-asks them (fixes "how large is your team?" twice).
                _known_facts: dict | None = None
                _existing_lead = db.execute(
                    select(Lead).where(
                        Lead.tenant_id == session.tenant_id,
                        Lead.conversation_id == session_id,
                    )
                ).scalar_one_or_none()
                if _existing_lead is not None:
                    _known_facts = {
                        "name": _existing_lead.name,
                        "email": _existing_lead.email,
                        "company": _existing_lead.company,
                        "team_size": _existing_lead.team_size,
                        "industry": _existing_lead.industry,
                    }
                # Surface the tenant's website (from their latest audit) so the agent
                # gives the real URL instead of a "[insert URL here]" placeholder.
                from app.db.models.audit import AuditReport

                _company_website = db.execute(
                    select(AuditReport.url)
                    .where(AuditReport.tenant_id == session.tenant_id)
                    .order_by(AuditReport.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                _sales_call_config = (
                    sales_agent_config
                    if _sales_close_allowed
                    else SalesConfigView(sales_agent_config, meeting_booking_enabled=False)
                )
                ai_response_text, _sales_meta, token_count, is_api_error = (
                    await rag_service.generate_sales_response(
                        prompt=prompt_components,
                        config=_sales_call_config,
                        model_name=settings.OPENAI_LLM_MODEL,
                        known_facts=_known_facts,
                        company_website=_company_website,
                        image_urls=_turn_image_urls,
                        **catalog_sales_kwargs(_offerings),
                    )
                )
            else:
                _sales_meta = None
                ai_response_text, token_count, is_api_error = (
                    await rag_service.generate_ai_response(
                        prompt=prompt_components,
                        max_tokens=tenant_ai_config.max_response_tokens,
                        model_name=settings.OPENAI_LLM_MODEL,
                        image_urls=_turn_image_urls,
                    )
                )

            # Offerings price guard: every reply built on a catalog block is checked in code;
            # one regeneration, then a reply built from the rows (never an empty reply).
            _guard_record: dict | None = None
            if _offerings is not None and not is_api_error:

                async def _regenerate(note: str):  # same call type as the first attempt
                    _retry = prompt_components.model_copy(
                        update={"full_prompt": f"{prompt_components.full_prompt}\n\n{note}"}
                    )
                    if _sales_mode:
                        return await rag_service.generate_sales_response(
                            prompt=_retry,
                            config=_sales_call_config,
                            model_name=settings.OPENAI_LLM_MODEL,
                            known_facts=_known_facts,
                            company_website=_company_website,
                            image_urls=_turn_image_urls,
                            **catalog_sales_kwargs(_offerings),
                        )
                    _text, _tokens, _err = await rag_service.generate_ai_response(
                        prompt=_retry,
                        max_tokens=tenant_ai_config.max_response_tokens,
                        model_name=settings.OPENAI_LLM_MODEL,
                        image_urls=_turn_image_urls,
                    )
                    return _text, None, _tokens, _err

                _guarded = await guard_reply(
                    ai_response_text,
                    _offerings,
                    regenerate=_regenerate,
                    tokens=token_count,
                    sales_meta=_sales_meta,
                    customer_texts=customer_texts(conversation_history, message_data.message),
                    kb_text=_kb_text,
                    tier_amounts=tier_amounts(sales_agent_config if _sales_mode else None),
                )
                ai_response_text, token_count, _sales_meta = (
                    _guarded.reply,
                    _guarded.tokens,
                    _guarded.sales_meta,
                )
                _guard_record = _guarded.record
                if _guarded.offer_escalation:
                    escalation_decision.skip_quality_check = True
                    if escalation_decision.action == EscalationAction.NO_ESCALATION:
                        escalation_decision.action = EscalationAction.OFFER_ESCALATION
                        escalation_decision.reason = "Offerings guard fell back to a code reply"

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
        _cart: CartResolution | None = None  # offerings: the customer's chosen items, read live
        _cart_synced: InterestSync | None = None
        if _sales_mode and _sales_meta is not None:
            try:
                if _offerings is not None and _offerings.mode != "unavailable":
                    _cart = resolve_cart(
                        db, session.tenant_id, _sales_meta.get("signals"), _offerings
                    )
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
                    or bool(_cart and _cart.has_content)
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
                        source=lead_source(turn_channel(), linked_visitor),
                        utm=_utm,
                    )

                    _catalog_signals: dict = {}
                    if _cart is not None and _cart.has_content:
                        _cart_synced = record_cart(db, lead, _cart)
                        _catalog_signals = catalog_score_signals(_cart_synced)
                        if _cart_synced.handover:
                            # D1: a counter-offer is never answered by the AI; a human takes it
                            if escalation_decision.action == EscalationAction.NO_ESCALATION:
                                escalation_decision.action = EscalationAction.OFFER_ESCALATION
                                escalation_decision.reason = "Customer made a price offer"
                                escalation_decision.skip_quality_check = True
                            if "connect you" not in ai_response_text:
                                ai_response_text = f"{ai_response_text}\n\n{ASK_TEAM}"

                    _email_before = lead.email  # capture before enrich
                    _prior_score = lead.lead_score  # capture before enrich for sales_ready check

                    lead = lead_svc.enrich_from_chat(
                        lead,
                        intent=_sales_meta["intent"],
                        captured=_sales_meta["captured"],
                        signals={**_sales_meta["signals"], **_catalog_signals},
                    )

                    if _cart_synced is not None:
                        await alert_owner(db, lead, _cart_synced)

                    # Set lead_captured only on the turn the email first appears.
                    if not _email_before and lead.email:
                        _lead_captured_meta = {
                            "name": lead.name,
                            "email": lead.email,
                        }

                    # Dispatch SALES_READY_LEAD on first threshold crossing.
                    try:
                        if lead_svc.crossed_sales_ready(_prior_score, lead):
                            from app.services.notification_service import NotificationService
                            from app.services.notification_enqueue import enqueue_jobs_async
                            from app.db.models.notification_log import NotifEvent

                            _sr_jobs = NotificationService(db).dispatch(
                                session.tenant_id,
                                NotifEvent.SALES_READY_LEAD,
                                entity_type="lead",
                                entity_id=lead.id,
                            )
                            await enqueue_jobs_async(_sr_jobs)
                    except Exception as _sr_exc:  # noqa: BLE001
                        logger.error(
                            f"sales_ready notification dispatch failed (non-fatal): {_sr_exc}"
                        )

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
                    if prompt_components is not None and prompt_components.prompt_tokens is not None
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
        _offerings_shown: list[int] = []
        if _offerings is not None:
            _message_meta["offerings"], _offerings_shown = build_offerings_turn(
                db, session.tenant_id, _offerings, _guard_record, ai_response_text, _sales_meta
            )
            if _offerings.mode == "none":
                record_miss(db, session.tenant_id, message_data.message)
        if _sales_mode and _sales_meta is not None:
            _signals = _sales_meta.get("signals") or {}
            if (
                _sales_close_allowed
                and bool(_signals.get("offer_meeting"))
                and getattr(sales_agent_config, "meeting_booking_enabled", False)
            ):
                _message_meta["offer_meeting"] = True
            _catalog_quote: dict | None = None
            if (
                _sales_close_allowed
                and getattr(sales_agent_config, "quotation_enabled", False)
                and lead is not None
                and _cart is not None
                and _cart_synced is not None
            ):
                try:
                    _catalog_quote = await issue_quote(
                        db, lead, _cart, _cart_synced, _offerings.currency
                    )
                    if _catalog_quote is not None:
                        _message_meta["quote"] = _catalog_quote
                except Exception as _cq_exc:
                    logger.error(f"Catalog quote error (non-fatal): {_cq_exc}")
            if (
                _catalog_quote is None
                and _sales_close_allowed
                and bool(_signals.get("offer_quote"))
                and getattr(sales_agent_config, "quotation_enabled", False)
            ):
                try:
                    _ts_raw = (lead.team_size if lead is not None else None) or (
                        _sales_meta.get("captured") or {}
                    ).get("team_size")
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
                            _created_quote = QuoteService(db).create_quote(
                                session.tenant_id,
                                lead,
                                currency=_q["currency"],
                                line_items=_q["line_items"],
                                total=_q["total"],
                            )
                            _message_meta["quote"] = _q
                            try:
                                from app.services.notification_service import NotificationService
                                from app.services.notification_enqueue import enqueue_jobs_async
                                from app.db.models.notification_log import NotifEvent

                                _qn_jobs = NotificationService(db).dispatch(
                                    session.tenant_id,
                                    NotifEvent.QUOTE_GENERATED,
                                    entity_type="quote",
                                    entity_id=_created_quote.id,
                                )
                                await enqueue_jobs_async(_qn_jobs)
                            except Exception as _qn_exc:  # noqa: BLE001
                                logger.error(
                                    f"quote notification dispatch failed (non-fatal): {_qn_exc}"
                                )
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
            ticket_id = await _escalate_to_human(
                db,
                conversation_service=conversation_service,
                tenant_service=tenant_service,
                session=session,
                session_id=session_id,
                escalation_reason=escalation_reason,
                customer_query=message_data.message,
                conversation_history=conversation_history,
                contact_email=contact_email,
            )

        remember_shown(db, session_id, _offerings_shown)

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
    response_model=StaffSessionDetailResponse,
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

    session.messages = redact_staff_messages(session.messages)
    session.ratings = session_rating_briefs(db, session_id)
    return ConversationService.to_staff_session(session, read_presence(db, session_id))


@router.get(
    "/tenants/{tenant_id}/sessions",
    response_model=list[StaffSessionResponse],
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

    presence = read_presence_many(db, [s.id for s in sessions])
    return [ConversationService.to_staff_session(s, presence.get(s.id)) for s in sessions]


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
    return redact_staff_messages(messages)


@router.post(
    "/sessions/{session_id}/takeover",
    response_model=StaffSessionResponse,
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

    # Claim the ticket FIRST and atomically (conditional UPDATE, same primitive as
    # the mobile acknowledge): if another agent already holds it, or it is already
    # resolved/closed, answer 409 and leave the session's agent untouched.
    ticket = (
        db.query(Ticket)
        .filter(Ticket.session_id == session_id)
        .order_by(Ticket.created_at.desc())
        .first()
    )
    if ticket:
        ticket_id = ticket.id
        claimed = TicketService(db).claim_ticket(ticket_id, current_user.id)
        if claimed is None:
            db.expire_all()
            ticket = db.get(Ticket, ticket_id)
            handler = db.get(User, ticket.assigned_to) if ticket.assigned_to else None
            final = ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=AcknowledgeLost(
                    handled_by=handler.full_name if handler else "another agent",
                    reason="resolved" if final else "claimed",
                ).model_dump(),
            )
        logger.info("Assigned ticket to agent after takeover")

    updated_session = conversation_service.assign_agent_to_session(
        session_id=session_id,
        agent_id=current_user.id,
    )

    # Stop the supervisor/fallback ring for this ticket, as the mobile claim does.
    # A failed enqueue must never fail a takeover that has already succeeded.
    if ticket:
        try:
            await enqueue_clear_alert_async(ticket_id, current_user.id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to enqueue clear_alert after takeover")

    # Persist the SYSTEM "joined" notice and fan it out to the customer widget.
    # Shared with the mobile Agent Caller path (`_finish_claim`) so both clients
    # produce an identical join notice.
    await emit_agent_joined(db, updated_session, current_user)

    logger.info("Agent took over session")

    # Sync Redis I/O must not run on the event loop.
    presence = await run_in_threadpool(read_presence, db, updated_session.id)
    return ConversationService.to_staff_session(updated_session, presence)


@router.post(
    "/sessions/{session_id}/upload",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
async def upload_agent_chat_image(
    session_id: UUID,
    file: UploadFile = File(..., description="Image (JPEG, PNG, GIF, WebP), max 10MB"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
    _subscription_check: object = Depends(verify_subscription_active),
):
    """Agent image upload during takeover: same pipeline as the public upload (OUTBOUND).

    The per-tenant ``widget_allow_file_upload`` flag governs *customer* uploads only
    and deliberately does not apply here.
    """
    session = ConversationService(db).get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    verify_tenant_access(session.tenant_id, current_user)
    if current_user.role not in [UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only support agents or admins can upload agent images",
        )
    attachment = await _process_image_upload(file, session, AttachmentDirection.OUTBOUND, db)
    return _upload_response(attachment)


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

    # Validate attachments before persisting anything (all-or-nothing, 400 on misuse).
    if message_data.attachment_ids:
        try:
            conversation_service.get_linkable_attachments(session_id, message_data.attachment_ids)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    reply_snapshot = (
        resolve_for_session_id(db, session_id, message_data.reply_to_message_id)
        if message_data.reply_to_message_id
        else None
    )

    # Create message
    message = conversation_service.add_message(
        session_id=session_id,
        reply_to=reply_snapshot,
        # message_role stores enum NAMES (USER/ASSISTANT/SYSTEM/HUMAN_AGENT) — pass
        # the member, not `.value`. (The old `.value` "workaround" was inert:
        # SQLAlchemy stores the name regardless, which is why the mis-cased
        # `human_agent` enum label caused 500s until migration d1f2humanagentname.)
        role=MessageRole.HUMAN_AGENT,
        content=(message_data.message or "").strip() or IMAGE_ONLY_PLACEHOLDER,
        meta={
            "agent_id": str(current_user.id),
            "agent_name": current_user.full_name,
        },
    )

    attachments: list[AttachmentOut] = []
    if message_data.attachment_ids:
        try:
            rows = conversation_service.link_attachments(
                session_id, message.id, message_data.attachment_ids
            )
            db.commit()
        except ValueError as e:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
        attachments = [AttachmentOut.model_validate(r) for r in rows]
        message.attachments = attachments

    # Broadcast to customer + other agents (local + cross-worker)
    await connection_manager.fanout_event(
        session_id=session_id,
        message={
            "type": "agent_message",
            "message_id": str(message.id),
            "content": message.content,
            "agent_name": current_user.full_name,
            "timestamp": message.created_at.isoformat(),
            "attachments": attachment_frames(attachments),
            "reply_to": frame_reply_to(message.reply_to),
        },
        to_customer=True,
        to_agents=True,
        exclude_agent_id=current_user.id,
    )

    logger.info("Agent sent message in session")

    return message
