"""WhatsApp event handling: status flips, inbound customer messages, owner-typed messages.

Tenant is resolved ONLY from `session_key` here (server side); nothing in the payload can
name a tenant. Pure functions of `(db, event)` so they are testable without arq; the arq
job in `app.workers.whatsapp` is a thin wrapper.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels import identity
from app.core.config import settings
from app.db.models.conversation import ConversationSession, MessageRole, SessionStatus
from app.db.models.tenant import TenantCapabilities
from app.db.models.visitor import Visitor, VisitorSource
from app.db.models.whatsapp import (
    WhatsAppChat,
    WhatsAppConnection,
    WhatsAppInboundMessage,
    WhatsAppStatus,
)
from app.schemas.conversation import ConversationSessionCreate, MessageRequest
from app.schemas.explore import ConversationSource
from app.services import whatsapp_enqueue
from app.services.conversation_service import (
    ConversationService,
    message_channel,
    new_session_attribution,
)
from app.utils.datetime import ensure_utc_aware, utcnow_aware

# Fixed fallback for anything that is not text (media is out of scope for the demo slice).
OTHER_KIND_REPLY = (
    "Sorry, I can only read text messages for now. Please type your question and I'll help."
)
# Transcript stand-in for a photo / voice note / file: the bytes are not read in this slice, but agents
# must see that something arrived (and that the customer was told to type instead).
UNSUPPORTED_MEDIA_PLACEHOLDER = (
    "[The customer sent a photo, voice note or file. Media is not supported on WhatsApp yet, "
    "so Luran could not read it.]"
)
PLAN_ALERT_DEBOUNCE = timedelta(hours=6)

_USABLE_SESSION = (SessionStatus.ACTIVE, SessionStatus.ESCALATED, SessionStatus.ABANDONED)

_DISCONNECT_REASONS = {
    401: "logged_out_from_phone",
    440: "connection_replaced",
    515: "restart_required",
}


def get_connection_by_session_key(db: Session, session_key: str) -> WhatsAppConnection | None:
    return db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.session_key == session_key)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- dispatch
async def handle_event(db: Session, event: dict) -> str:
    """Process one validated webhook event. Returns a short outcome label (for tests/logs)."""
    conn = get_connection_by_session_key(db, event["session_key"])
    if conn is None:
        logger.info("whatsapp event dropped: unknown session")
        return "dropped_unknown_session"
    etype = event["type"]
    if etype == "session.status":
        return handle_status(db, conn, event)
    if etype == "message.inbound":
        return await handle_inbound(db, conn, event)
    if etype == "message.outbound_external":
        return await handle_outbound_external(db, conn, event)
    return "ignored_type"


# --------------------------------------------------------------------------- status
def handle_status(db: Session, conn: WhatsAppConnection, event: dict) -> str:
    """Flip the connection status from a sidecar report. Never re-pairs automatically."""
    try:
        status = WhatsAppStatus(event["status"])
    except ValueError:
        logger.warning("whatsapp status event with unknown status ignored")
        return "ignored_unknown_status"
    now = utcnow_aware()
    conn.status = status.value
    if status == WhatsAppStatus.CONNECTED:
        conn.last_connected_at = now
    elif status in (
        WhatsAppStatus.LOGGED_OUT,
        WhatsAppStatus.FAILED,
        WhatsAppStatus.RECONNECTING,
    ):
        code = event.get("code")
        conn.last_disconnect_at = now
        conn.last_disconnect_reason = _DISCONNECT_REASONS.get(
            code, f"closed_{code}" if code is not None else status.value
        )
        if status in (WhatsAppStatus.LOGGED_OUT, WhatsAppStatus.FAILED):
            # TODO(beta): email tenant admins + in-app notice (debounced, not for deliberate
            # disconnects). The demo only records it.
            logger.warning("whatsapp connection {} went {}", conn.id, status.value)
    db.commit()
    return f"status_{status.value}"


# --------------------------------------------------------------------------- helpers
def _new_visitor(db: Session, tenant_id: UUID, push_name: str | None) -> Visitor:
    now = utcnow_aware()
    visitor = Visitor(
        tenant_id=tenant_id,
        public_token=secrets.token_urlsafe(32),
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WHATSAPP,
        # NOTE: no `phone`: the Visitor listener would store an UNSALTED SHA-256 of it.
        name=(push_name or "").strip()[:255] or None,
    )
    db.add(visitor)
    db.flush()
    return visitor


def _get_or_create_chat(
    db: Session, conn: WhatsAppConnection, chat_ref: str
) -> tuple[WhatsAppChat, bool]:
    key = identity.contact_hmac(chat_ref)
    chat = db.execute(
        select(WhatsAppChat).where(
            WhatsAppChat.connection_id == conn.id, WhatsAppChat.contact_hmac == key
        )
    ).scalar_one_or_none()
    if chat is not None:
        return chat, False
    chat = WhatsAppChat(
        tenant_id=conn.tenant_id, connection_id=conn.id, contact_hmac=key, contact_ref=chat_ref
    )
    db.add(chat)
    db.flush()
    return chat, True


def _ensure_visitor(db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, push_name) -> Visitor:  # type: ignore[no-untyped-def]
    visitor = db.get(Visitor, chat.visitor_id) if chat.visitor_id else None
    if visitor is None:
        visitor = _new_visitor(db, conn.tenant_id, push_name)
        chat.visitor_id = visitor.id
    elif push_name and not visitor.name:
        visitor.name = push_name.strip()[:255] or None
    return visitor


def _record_message_id(db: Session, conn: WhatsAppConnection, provider_message_id: str) -> bool:
    """Idempotent insert. False = already seen (duplicate delivery or replay)."""
    try:
        with db.begin_nested():
            db.add(
                WhatsAppInboundMessage(
                    connection_id=conn.id,
                    provider_message_id=provider_message_id,
                    received_at=utcnow_aware(),
                )
            )
        return True
    except IntegrityError:
        return False


def _forget_message_id(db: Session, conn: WhatsAppConnection, provider_message_id: str) -> None:
    db.rollback()
    db.execute(
        delete(WhatsAppInboundMessage).where(
            WhatsAppInboundMessage.connection_id == conn.id,
            WhatsAppInboundMessage.provider_message_id == provider_message_id,
        )
    )
    db.commit()


def _usable_session(db: Session, conn: WhatsAppConnection, chat: WhatsAppChat):  # type: ignore[no-untyped-def]
    """The chat's current conversation unless it is resolved (then a new one starts)."""
    if not chat.session_id:
        return None
    session = db.get(ConversationSession, chat.session_id)
    if session is None or session.tenant_id != conn.tenant_id:
        return None
    return session if session.status in _USABLE_SESSION else None


def _ensure_session(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, user_identifier: str
) -> ConversationSession:
    """Existing usable session, else a new WhatsApp-sourced one (store-only paths)."""
    session = _usable_session(db, conn, chat)
    if session is not None:
        if session.status == SessionStatus.ABANDONED:
            session.status = SessionStatus.ACTIVE
            db.commit()
        return session
    created = ConversationService(db).create_session(
        ConversationSessionCreate(
            tenant_id=conn.tenant_id,
            user_identifier=user_identifier,
            visitor_id=chat.visitor_id,
            source=ConversationSource.WHATSAPP.value,
            source_meta={"channel_connection_id": str(conn.id), "provider": conn.provider},
        )
    )
    chat.session_id = created.id
    db.commit()
    return db.get(ConversationSession, created.id)  # type: ignore[return-value]


def _tenant_may_use(db: Session, tenant_id: UUID) -> bool:
    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
    ).scalar_one_or_none()
    return settings.WHATSAPP_ENABLED and bool(caps and caps.can_use_whatsapp)


async def _enqueue_reply(conn: WhatsAppConnection, chat: WhatsAppChat, text: str, key: str) -> None:
    await whatsapp_enqueue.enqueue_send(conn.id, chat.id, text, key, guard=True)


# --------------------------------------------------------------------------- inbound
async def handle_inbound(db: Session, conn: WhatsAppConnection, event: dict) -> str:
    from app.api.v1.endpoints.chat import _fanout_customer_message, send_message
    from app.db.models.conversation import Message

    if not _tenant_may_use(db, conn.tenant_id):  # kill switch / revoked grant
        return "dropped_disabled"

    message_id = event["message_id"]
    if not _record_message_id(db, conn, message_id):
        return "duplicate"
    db.commit()

    try:
        now = utcnow_aware()
        chat, _created = _get_or_create_chat(db, conn, event["from_ref"])
        chat.last_inbound_at = now
        if event.get("from_ref") and chat.contact_ref != event["from_ref"]:
            chat.contact_ref = event["from_ref"]
        db.commit()

        human_until = ensure_utc_aware(chat.human_until)
        paused = human_until is not None and human_until > now
        text = (event.get("text") or "").strip()

        if event["kind"] != "text" or not text:
            silent = paused or not conn.ai_enabled  # no automatic replies while a human owns it
            if not silent:
                # Enqueue first: the send is idempotent on its key, so an arq retry after a
                # storage failure cannot double-send, and a failed enqueue stores nothing.
                await _enqueue_reply(conn, chat, OTHER_KIND_REPLY, f"other:{message_id}")
            _ensure_visitor(db, conn, chat, event.get("push_name"))
            db.commit()
            await _store_non_text(
                db,
                conn,
                chat,
                f"wa_{chat.contact_hmac[:24]}",
                not silent,
                _fanout_customer_message,
                Message,
            )
            return "stored_other_while_paused" if silent else "other_kind_reply"

        visitor = _ensure_visitor(db, conn, chat, event.get("push_name"))
        db.commit()
        user_identifier = f"wa_{chat.contact_hmac[:24]}"

        if paused or not conn.ai_enabled:
            return await _store_without_ai(
                db, conn, chat, text, user_identifier, paused, _fanout_customer_message, Message
            )

        session = _usable_session(db, conn, chat)
        request = MessageRequest(
            message=text,
            session_id=session.id if session else None,
            tenant_id=conn.tenant_id,
            user_identifier=user_identifier,
            metadata={"visitor_token": visitor.public_token, "entry_point": "whatsapp"},
        )
        attribution = new_session_attribution.set(
            (
                ConversationSource.WHATSAPP.value,
                {"channel_connection_id": str(conn.id), "provider": conn.provider},
            )
        )
        channel = message_channel.set("whatsapp")
        try:
            response = await send_message(request, db)
        except HTTPException as exc:
            if exc.status_code == 402:
                return await _plan_limit(db, conn, chat, text, user_identifier)
            raise
        finally:
            message_channel.reset(channel)
            new_session_attribution.reset(attribution)

        chat.session_id = response.session_id
        db.commit()
        if response.message.role == MessageRole.USER:
            return "stored_human_owns_session"  # escalated: a human answers, no AI reply
        content = (response.message.content or "").strip()
        if content:
            await _enqueue_reply(conn, chat, content, f"ai:{response.message.id}")
        return "ai_reply"
    except Exception:
        # Let arq retry: forget the dedupe row so the retry is not skipped as a duplicate.
        _forget_message_id(db, conn, message_id)
        raise


async def _store_without_ai(
    db: Session,
    conn: WhatsAppConnection,
    chat: WhatsAppChat,
    text: str,
    user_identifier: str,
    paused: bool,
    fanout,  # type: ignore[no-untyped-def]
    message_model,  # type: ignore[no-untyped-def]
) -> str:
    """Owner pause / AI disabled: persist the customer's message and show it to agents."""
    session = _ensure_session(db, conn, chat, user_identifier)
    channel = message_channel.set("whatsapp")
    try:
        msg = ConversationService(db).add_message(
            session_id=session.id, role=MessageRole.USER, content=text
        )
    finally:
        message_channel.reset(channel)
    await fanout(db, session.id, select(message_model).where(message_model.id == msg.id))
    return "stored_owner_paused" if paused else "stored_ai_disabled"


async def _store_non_text(
    db: Session,
    conn: WhatsAppConnection,
    chat: WhatsAppChat,
    user_identifier: str,
    replied: bool,
    fanout,  # type: ignore[no-untyped-def]
    message_model,  # type: ignore[no-untyped-def]
) -> None:
    """Persist what happened to a non-text message so the transcript is complete: a stand-in for
    the customer's media, plus the fixed reply when one was sent. Agents are shown the customer side.
    """
    session = _ensure_session(db, conn, chat, user_identifier)
    channel = message_channel.set("whatsapp")
    try:
        service = ConversationService(db)
        msg = service.add_message(
            session_id=session.id, role=MessageRole.USER, content=UNSUPPORTED_MEDIA_PLACEHOLDER
        )
        if replied:
            service.add_message(
                session_id=session.id,
                role=MessageRole.ASSISTANT,
                content=OTHER_KIND_REPLY,
                meta={"source": "whatsapp_media_fallback"},
            )
    finally:
        message_channel.reset(channel)
    await fanout(db, session.id, select(message_model).where(message_model.id == msg.id))


async def _plan_limit(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, text: str, user_identifier: str
) -> str:
    """HTTP 402 from the pipeline (conversation limit / trial / subscription): store the
    message when a conversation exists, say NOTHING to the customer, alert the tenant."""
    session = _usable_session(db, conn, chat)
    if session is not None:
        ConversationService(db).add_message(
            session_id=session.id, role=MessageRole.USER, content=text
        )
    now = utcnow_aware()
    last_alert = ensure_utc_aware(conn.last_alert_at)
    if last_alert is None or now - last_alert > PLAN_ALERT_DEBOUNCE:
        conn.last_alert_at = now
        db.commit()
        # TODO(beta): email tenant admins that WhatsApp messages are blocked by their plan.
        logger.warning("whatsapp plan limit reached for tenant {}", conn.tenant_id)
    return "plan_limit"


# --------------------------------------------------------------------------- owner typed
async def handle_outbound_external(db: Session, conn: WhatsAppConnection, event: dict) -> str:
    """The owner typed from the phone / WhatsApp Web: pause the AI on that chat.

    Privacy: when this contact has never messaged the AI we only create a minimal chat row
    to hold the pause (no visitor, no conversation, no text stored) so personal messages the
    owner sends from the same number are not copied into Luran.
    """
    from app.core.websocket import connection_manager

    if not _tenant_may_use(db, conn.tenant_id):
        return "dropped_disabled"
    message_id = event["message_id"]
    if not _record_message_id(db, conn, message_id):
        return "duplicate"
    db.commit()
    try:
        now = utcnow_aware()
        chat, created = _get_or_create_chat(db, conn, event["to_ref"])
        chat.human_until = now + timedelta(minutes=settings.WHATSAPP_OWNER_PAUSE_MINUTES)
        db.commit()
        if created or chat.last_inbound_at is None:
            # The contact never wrote to the AI (also on the owner's 2nd, 3rd... message to them):
            # keep only the pause, never the text.
            return "paused_no_transcript"

        text = (event.get("text") or "").strip()
        if not text:
            return "paused_empty"
        session = _ensure_session(db, conn, chat, f"wa_{chat.contact_hmac[:24]}")
        msg = ConversationService(db).add_message(
            session_id=session.id,
            role=MessageRole.HUMAN_AGENT,
            content=text,
            meta={"source": "owner_phone"},
        )
        # Agents only: to_customer=True would echo the owner's own text back to WhatsApp.
        await connection_manager.fanout_event(
            session_id=session.id,
            message={
                "type": "agent_message",
                "message_id": str(msg.id),
                "content": text,
                "agent_name": "Owner (phone)",
                "timestamp": msg.created_at.isoformat(),
                "attachments": [],
                "reply_to": None,
            },
            to_customer=False,
            to_agents=True,
        )
        return "paused_stored"
    except Exception:
        _forget_message_id(db, conn, message_id)
        raise
